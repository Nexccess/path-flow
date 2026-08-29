from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DEFAULT_CAMPAIGN_ID = "PF-NAIL-001"

EXCLUDE_CONTACT_STATUSES = {
    "NO_CONTACT",
    "LEGACY_NO_CONTACT",
    "FETCH_FAILED",
}
EXCLUDE_SCREENING_PREFIXES = (
    "FORM_BLOCKED_CAPTCHA",
    "FORM_BLOCKED_POLICY",
    "FORM_BLOCKED_THIRD_PARTY",
    "FORM_BLOCKED_DYNAMIC",
)
MANUAL_REACHABLE_STATUSES = {
    "MANUAL_CHECK",
    "DISCOVERY_REQUIRED",
    "READY_LINE",
    "READY_INSTAGRAM",
    "READY_SMS",
}


@dataclass(frozen=True)
class ScoreResult:
    qualification_grade: str
    lead_score: int
    lead_priority: str
    exclusion_reason: str | None
    score_breakdown: dict[str, int]


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def migrate_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    additions = {
        "qualification_grade": "TEXT",
        "lead_score": "INTEGER",
        "lead_priority": "TEXT",
        "qualification_reason": "TEXT",
        "score_breakdown": "TEXT",
        "qualified_at": "TEXT",
        "google_rating": "REAL",
        "google_review_count": "INTEGER",
    }
    for name, spec in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {spec}")
    conn.commit()


def as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_excluded(contact_status: str | None, screening_status: str | None) -> tuple[bool, str | None]:
    contact = (contact_status or "").upper()
    screening = (screening_status or "").upper()

    if contact in EXCLUDE_CONTACT_STATUSES:
        return True, f"contact_status={contact}"
    if any(screening.startswith(prefix) for prefix in EXCLUDE_SCREENING_PREFIXES):
        return True, f"screening_status={screening}"
    return False, None


def qualification_grade(
    contact_status: str | None,
    screening_status: str | None,
    send_allowed: int | None,
    email: str | None,
    form_url: str | None,
    line_url: str | None,
    instagram_url: str | None,
) -> tuple[str, str | None]:
    """Return the contactability gate result.

    A = AUTO_SENDABLE
    C = MANUAL_REACHABLE
    D = EXCLUDE

    Grade B is intentionally unused in v1.2. Earlier prototypes mixed
    'requires final check' with commercial priority, which made contactability
    and scoring ambiguous.
    """
    excluded, reason = is_excluded(contact_status, screening_status)
    if excluded:
        return "D", reason

    contact = (contact_status or "").upper()
    screening = (screening_status or "").upper()
    allowed = bool(send_allowed)

    if allowed and contact == "READY_EMAIL" and email:
        return "A", "auto_sendable_email"
    if allowed and contact == "READY_FORM" and form_url and screening == "FORM_AUTO_READY":
        return "A", "auto_sendable_form"

    if contact in MANUAL_REACHABLE_STATUSES or line_url or instagram_url:
        return "C", "manual_reachable"

    # A discovered email/form that has not passed the automatic-send gate is
    # still reachable, but must not be promoted into the automatic queue.
    if (contact == "READY_EMAIL" and email) or (contact == "READY_FORM" and form_url):
        return "C", "manual_reachable_pending_validation"

    return "D", "no_valid_sales_channel"


def calculate_auto_sendable_score(
    google_rating: float | None,
    google_review_count: int | None,
) -> tuple[int, str, dict[str, int]]:
    """Prioritize only after a lead has passed the contactability gate.

    The v1 rule is deliberately explainable and conservative:
      - AUTO_SENDABLE baseline: 50 points => MEDIUM
      - Google rating >= 4.5: +10
      - Google reviews >= 50: +10
      - HIGH requires both value signals (70 points)

    Contact-channel count is not used as a value signal; having more channels
    changes reachability/redundancy, not the commercial attractiveness itself.
    """
    breakdown: dict[str, int] = {"auto_sendable": 50}

    if google_rating is not None and google_rating >= 4.5:
        breakdown["rating_ge_4_5"] = 10
    if google_review_count is not None and google_review_count >= 50:
        breakdown["reviews_ge_50"] = 10

    score = sum(breakdown.values())
    priority = "HIGH" if score >= 70 else "MEDIUM"
    return score, priority, breakdown


def score_lead(row: sqlite3.Row) -> ScoreResult:
    grade, reason = qualification_grade(
        row["contact_status"],
        row["screening_status"],
        row["send_allowed"],
        row["email"],
        row["contact_form_url"],
        row["line_url"],
        row["instagram_url"],
    )

    if grade == "D":
        return ScoreResult(
            qualification_grade="D",
            lead_score=0,
            lead_priority="EXCLUDE",
            exclusion_reason=reason,
            score_breakdown={},
        )

    if grade == "C":
        return ScoreResult(
            qualification_grade="C",
            lead_score=25,
            lead_priority="LOW",
            exclusion_reason=reason,
            score_breakdown={"manual_reachable": 25},
        )

    score, priority, breakdown = calculate_auto_sendable_score(
        as_float(row["google_rating"]),
        as_int(row["google_review_count"]),
    )
    return ScoreResult(
        qualification_grade="A",
        lead_score=score,
        lead_priority=priority,
        exclusion_reason=reason,
        score_breakdown=breakdown,
    )


def run(
    db: Path,
    campaign_id: str,
    apply: bool = False,
    limit: int | None = None,
    only_unscored: bool = False,
) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        migrate_columns(conn)
        sql = """
            SELECT campaign_id, store_id, store_name,
                   contact_status, screening_status, send_allowed,
                   email, contact_form_url, line_url, instagram_url,
                   google_rating, google_review_count
            FROM leads
            WHERE campaign_id=?
        """
        params: list[object] = [campaign_id]
        if only_unscored:
            sql += " AND lead_priority IS NULL"
        sql += " ORDER BY store_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "EXCLUDE": 0}
        grades = {"A": 0, "B": 0, "C": 0, "D": 0}

        for row in rows:
            result = score_lead(row)
            counts[result.lead_priority] += 1
            grades[result.qualification_grade] += 1

            print(
                f"{result.lead_priority}\t{result.lead_score}\t{result.qualification_grade}"
                f"\t{row['store_id']}\t{row['store_name']}"
            )

            if apply:
                conn.execute(
                    """
                    UPDATE leads
                    SET qualification_grade=?, lead_score=?, lead_priority=?,
                        qualification_reason=?, score_breakdown=?, qualified_at=?, updated_at=?
                    WHERE campaign_id=? AND store_id=?
                    """,
                    (
                        result.qualification_grade,
                        result.lead_score,
                        result.lead_priority,
                        result.exclusion_reason,
                        json.dumps(result.score_breakdown, ensure_ascii=False, sort_keys=True),
                        now_iso(),
                        now_iso(),
                        campaign_id,
                        row["store_id"],
                    ),
                )

        if apply:
            conn.commit()

        summary = {
            "campaign_id": campaign_id,
            "targets": len(rows),
            "applied": apply,
            "priority": counts,
            "qualification": grades,
        }
        print("summary=" + json.dumps(summary, ensure_ascii=False))
        return summary
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Contactability gate + deterministic lead prioritization for Nexccess Revenue Engine v1."
    )
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    p.add_argument("--limit", type=int)
    p.add_argument("--only-unscored", action="store_true")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Persist qualification and scoring results. Default is dry-run.",
    )
    args = p.parse_args()
    run(
        db=args.db,
        campaign_id=args.campaign_id,
        apply=args.apply,
        limit=args.limit,
        only_unscored=args.only_unscored,
    )


if __name__ == "__main__":
    main()

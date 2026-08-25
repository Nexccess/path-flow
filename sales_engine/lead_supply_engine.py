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
LOW_REVIEW_STATUSES = {
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
    excluded, reason = is_excluded(contact_status, screening_status)
    if excluded:
        return "D", reason

    contact = (contact_status or "").upper()
    screening = (screening_status or "").upper()
    allowed = bool(send_allowed)

    if allowed and email:
        return "A", "validated_email"
    if allowed and form_url and screening == "FORM_AUTO_READY":
        return "A", "validated_form"

    if contact == "READY_EMAIL" and email:
        return "B", "email_requires_final_send_check"
    if contact == "READY_FORM" and form_url:
        return "B", "form_requires_final_send_check"

    if contact in LOW_REVIEW_STATUSES or line_url or instagram_url:
        return "C", "reachable_but_not_auto_sendable"

    return "D", "no_valid_sales_channel"


def calculate_score(
    grade: str,
    email: str | None,
    form_url: str | None,
    line_url: str | None,
    instagram_url: str | None,
    google_rating: float | None,
    google_review_count: int | None,
) -> tuple[int, str, dict[str, int]]:
    breakdown: dict[str, int] = {}

    qualification_points = {"A": 30, "B": 25, "C": 15, "D": 5}[grade]
    breakdown["qualification"] = qualification_points

    if email:
        breakdown["email"] = 20
    if form_url:
        breakdown["form"] = 20
    if line_url:
        breakdown["line"] = 10
    if instagram_url:
        breakdown["instagram"] = 5

    if google_rating is not None and google_rating >= 4.5:
        breakdown["rating"] = 10

    if google_review_count is not None:
        if google_review_count >= 100:
            breakdown["reviews"] = 10
        elif google_review_count >= 50:
            breakdown["reviews"] = 5

    # v1.1 timing bonus: email/form can be scheduled into the approved send windows.
    if email or form_url:
        breakdown["timing"] = 10

    score = sum(breakdown.values())
    if score >= 90:
        priority = "HOT"
    elif score >= 70:
        priority = "HIGH"
    elif score >= 50:
        priority = "MEDIUM"
    else:
        priority = "LOW"
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

    score, priority, breakdown = calculate_score(
        grade,
        row["email"],
        row["contact_form_url"],
        row["line_url"],
        row["instagram_url"],
        as_float(row["google_rating"]),
        as_int(row["google_review_count"]),
    )
    return ScoreResult(
        qualification_grade=grade,
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
        counts = {"HOT": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "EXCLUDE": 0}
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
        description="Qualification + deterministic lead scoring for Nexccess Revenue Engine v1."
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

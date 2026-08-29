from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

VERSION = "screening-v1"

PORTAL_HOST_HINTS = (
    "beauty.hotpepper.jp",
    "b.hpr.jp",
    "tabelog.com",
    "maps.google.",
    "google.com/maps",
)
BOOKING_HINTS = (
    "/reserve", "/reservation", "/booking", "/book-online", "/trial",
    "予約", "体験予約", "counseling", "reservations/create",
)
BAD_EMAIL_LOCAL_HINTS = (
    "example", "sample", "test", "noreply", "no-reply", "donotreply",
    "instagram", "facebook", "twitter", "sentry",
)
BAD_EMAIL_DOMAIN_HINTS = (
    "wixpress.com", "sentry.io", "yourdomain.jp", "example.com", "example.jp",
)
EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    route: str | None = None
    detail: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def valid_email(value: str | None) -> bool:
    value = clean(value)
    if not value or not EMAIL_RE.fullmatch(value):
        return False
    low = value.lower()
    local, domain = low.rsplit("@", 1)
    if any(h in local for h in BAD_EMAIL_LOCAL_HINTS):
        return False
    if any(h in domain for h in BAD_EMAIL_DOMAIN_HINTS):
        return False
    return True


def host_of(url: str | None) -> str:
    try:
        return urlparse(clean(url) or "").netloc.lower()
    except Exception:
        return ""


def is_portal(url: str | None) -> bool:
    low = (clean(url) or "").lower()
    host = host_of(url)
    return any(h in low or h in host for h in PORTAL_HOST_HINTS)


def looks_booking_only(url: str | None) -> bool:
    low = (clean(url) or "").lower()
    return any(h.lower() in low for h in BOOKING_HINTS)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screening_decisions (
          lead_id INTEGER PRIMARY KEY,
          decision TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          route TEXT,
          detail TEXT,
          screening_version TEXT NOT NULL,
          decided_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hold_pool (
          lead_id INTEGER PRIMARY KEY,
          reason_code TEXT NOT NULL,
          detail TEXT,
          status TEXT NOT NULL DEFAULT 'OPEN',
          screening_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS close_audit (
          lead_id INTEGER PRIMARY KEY,
          reason_code TEXT NOT NULL,
          detail TEXT,
          screening_version TEXT NOT NULL,
          closed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS screening_runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          campaign_id TEXT NOT NULL,
          total INTEGER NOT NULL,
          go_count INTEGER NOT NULL,
          hold_count INTEGER NOT NULL,
          close_count INTEGER NOT NULL,
          applied INTEGER NOT NULL,
          screening_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sales_queue (
          queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          campaign_id TEXT NOT NULL,
          company_name TEXT,
          website_url TEXT,
          status TEXT NOT NULL DEFAULT 'READY',
          created_at TEXT NOT NULL,
          bridge_version TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_screening_decision ON screening_decisions(decision);
        CREATE INDEX IF NOT EXISTS idx_hold_reason ON hold_pool(reason_code, status);
        CREATE INDEX IF NOT EXISTS idx_close_reason ON close_audit(reason_code);
        CREATE INDEX IF NOT EXISTS idx_sales_queue_campaign_lead ON sales_queue(campaign_id, lead_id);
        """
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def classify(row: sqlite3.Row, target_categories: set[str], target_areas: set[str]) -> Decision:
    category = clean(row["category"])
    area = clean(row["area"])
    website = clean(row["website_url"])
    email = clean(row["legacy_email"])
    form = clean(row["legacy_form_url"])

    if target_categories and category not in target_categories:
        return Decision("CLOSE", "OUT_OF_TARGET", detail=f"category={category or '-'}")
    if target_areas and area not in target_areas:
        return Decision("CLOSE", "OUT_OF_TARGET", detail=f"area={area or '-'}")

    if valid_email(email):
        return Decision("GO", "VALID_EMAIL", route="email")

    portal_form = bool(form and is_portal(form))
    portal_website = bool(website and is_portal(website))

    if form and not portal_form:
        if looks_booking_only(form):
            return Decision("HOLD", "AMBIGUOUS_FORM", route="form", detail=form)
        return Decision("GO", "CONTACT_FORM", route="form")

    if portal_form and portal_website:
        return Decision("CLOSE", "PORTAL_ONLY", detail=form or website)

    if email and not valid_email(email):
        if website:
            return Decision("HOLD", "INVALID_EMAIL_RECHECK", detail=email)
        return Decision("CLOSE", "INVALID_EMAIL", detail=email)

    if website:
        if portal_website:
            return Decision("HOLD", "PORTAL_WEBSITE_REVIEW", detail=website)
        return Decision("HOLD", "NO_CONFIRMED_CONTACT_ROUTE", detail=website)

    if form and portal_form:
        return Decision("CLOSE", "PORTAL_ONLY", detail=form)

    return Decision("CLOSE", "NO_CONTACT_ROUTE")


def load_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    cols = table_columns(conn, "leads")
    required = {"lead_id", "company_name", "category", "area", "website_url", "legacy_email", "legacy_form_url"}
    missing = required - cols
    if missing:
        raise SystemExit(f"leads table missing required columns: {sorted(missing)}")

    sql = """
        SELECT lead_id, company_name, category, area, website_url,
               legacy_email, legacy_form_url
        FROM leads
        ORDER BY lead_id
    """
    params: list[object] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def persist_decision(conn: sqlite3.Connection, row: sqlite3.Row, decision: Decision, campaign_id: str) -> None:
    ts = now_iso()
    lead_id = int(row["lead_id"])
    conn.execute(
        """
        INSERT INTO screening_decisions(lead_id, decision, reason_code, route, detail, screening_version, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lead_id) DO UPDATE SET
          decision=excluded.decision,
          reason_code=excluded.reason_code,
          route=excluded.route,
          detail=excluded.detail,
          screening_version=excluded.screening_version,
          decided_at=excluded.decided_at
        """,
        (lead_id, decision.status, decision.reason, decision.route, decision.detail, VERSION, ts),
    )

    if decision.status == "GO":
        conn.execute("DELETE FROM hold_pool WHERE lead_id=?", (lead_id,))
        conn.execute("DELETE FROM close_audit WHERE lead_id=?", (lead_id,))
        exists = conn.execute(
            "SELECT 1 FROM sales_queue WHERE campaign_id=? AND lead_id=? LIMIT 1",
            (campaign_id, lead_id),
        ).fetchone()
        if not exists:
            conn.execute(
                """
                INSERT INTO sales_queue(lead_id, campaign_id, company_name, website_url, status, created_at, bridge_version)
                VALUES (?, ?, ?, ?, 'READY', ?, ?)
                """,
                (lead_id, campaign_id, clean(row["company_name"]), clean(row["website_url"]), ts, VERSION),
            )
    elif decision.status == "HOLD":
        conn.execute(
            """
            INSERT INTO hold_pool(lead_id, reason_code, detail, status, screening_version, created_at, updated_at)
            VALUES (?, ?, ?, 'OPEN', ?, ?, ?)
            ON CONFLICT(lead_id) DO UPDATE SET
              reason_code=excluded.reason_code,
              detail=excluded.detail,
              status='OPEN',
              screening_version=excluded.screening_version,
              updated_at=excluded.updated_at
            """,
            (lead_id, decision.reason, decision.detail, VERSION, ts, ts),
        )
        conn.execute("DELETE FROM close_audit WHERE lead_id=?", (lead_id,))
    else:
        conn.execute("DELETE FROM hold_pool WHERE lead_id=?", (lead_id,))
        conn.execute(
            """
            INSERT INTO close_audit(lead_id, reason_code, detail, screening_version, closed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lead_id) DO UPDATE SET
              reason_code=excluded.reason_code,
              detail=excluded.detail,
              screening_version=excluded.screening_version,
              closed_at=excluded.closed_at
            """,
            (lead_id, decision.reason, decision.detail, VERSION, ts),
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Lightweight GO/HOLD/CLOSE screening for Lead Supply v1.")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--campaign-id", default="LEAD-SUPPLY-V1")
    p.add_argument("--category", action="append", default=[])
    p.add_argument("--area", action="append", default=[])
    p.add_argument("--limit", type=int)
    p.add_argument("--apply", action="store_true", help="Persist decisions and enqueue GO leads. Default is dry-run.")
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        rows = load_rows(conn, args.limit)
        decisions: list[tuple[sqlite3.Row, Decision]] = []
        for row in rows:
            decisions.append((row, classify(row, set(args.category), set(args.area))))

        counts = Counter(d.status for _, d in decisions)
        reasons = Counter(d.reason for _, d in decisions)

        if args.apply:
            ensure_schema(conn)
            for row, decision in decisions:
                persist_decision(conn, row, decision, args.campaign_id)
            conn.execute(
                """
                INSERT INTO screening_runs(campaign_id, total, go_count, hold_count, close_count, applied, screening_version, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (args.campaign_id, len(decisions), counts["GO"], counts["HOLD"], counts["CLOSE"], VERSION, now_iso()),
            )
            conn.commit()

        output = {
            "screening_version": VERSION,
            "db": str(args.db),
            "campaign_id": args.campaign_id,
            "applied": args.apply,
            "total": len(decisions),
            "decision": {k: counts.get(k, 0) for k in ("GO", "HOLD", "CLOSE")},
            "decision_pct": {
                k: round((counts.get(k, 0) / len(decisions) * 100), 2) if decisions else 0.0
                for k in ("GO", "HOLD", "CLOSE")
            },
            "reason_code": dict(reasons.most_common()),
            "policy": {
                "go": "clear usable contact route",
                "hold": "ambiguous/reviewable; never blocks GO flow",
                "close": "clear NG only",
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

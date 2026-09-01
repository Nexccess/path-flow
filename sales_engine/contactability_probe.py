from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTELLIGENCE_DB = REPO_ROOT / "lead_intelligence" / "data" / "working" / "lead_intelligence.db"
DEFAULT_SALES_DB = REPO_ROOT / "sales_engine.db"
DEFAULT_LEGACY_DB = REPO_ROOT / "leads_database.db"

CONTACT_COLUMNS = [
    "lead_id", "place_id", "google_place_id", "company_name", "store_name",
    "website_url", "store_url", "email", "form_url", "contact_form_url",
    "contact_status", "primary_channel", "send_allowed", "screening_status",
    "contact_source_url", "contact_confidence"
]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def print_rows(db_path: Path, label: str, lead_ids: list[int]) -> None:
    print(f"=== {label}: {db_path} ===")
    if not db_path.exists():
        print("NOT_FOUND")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "leads"):
            print("NO_LEADS_TABLE")
            return

        cols = columns(conn, "leads")
        selected = [c for c in CONTACT_COLUMNS if c in cols]
        if not selected:
            print("NO_CONTACT_COLUMNS")
            return

        key = "lead_id" if "lead_id" in cols else ("store_id" if "store_id" in cols else None)
        if key is None:
            print("NO_LEAD_KEY")
            print("columns=" + ",".join(cols))
            return

        placeholders = ",".join("?" for _ in lead_ids)
        sql = f"SELECT {','.join(selected)} FROM leads WHERE {key} IN ({placeholders})"
        rows = conn.execute(sql, [str(x) if key == "store_id" else x for x in lead_ids]).fetchall()

        if not rows:
            print("NO_MATCHING_ROWS")
            return

        for row in rows:
            print(dict(row))
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only Contactability probe for selected leads.")
    p.add_argument("--lead-id", type=int, action="append", dest="lead_ids", required=True)
    p.add_argument("--intelligence-db", type=Path, default=DEFAULT_INTELLIGENCE_DB)
    p.add_argument("--sales-db", type=Path, default=DEFAULT_SALES_DB)
    p.add_argument("--legacy-db", type=Path, default=DEFAULT_LEGACY_DB)
    args = p.parse_args()

    print_rows(args.intelligence_db, "lead_intelligence", args.lead_ids)
    print_rows(args.sales_db, "sales_engine", args.lead_ids)
    print_rows(args.legacy_db, "legacy_leads", args.lead_ids)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import sqlite3
import sys

import screening_automation as base

_SOURCE: str | None = None
_original_load_rows = base.load_rows


def load_rows_by_source(conn: sqlite3.Connection, limit: int | None):
    if not _SOURCE:
        return _original_load_rows(conn, limit)

    cols = base.table_columns(conn, "leads")
    required = {
        "lead_id", "company_name", "category", "area", "website_url",
        "legacy_email", "legacy_form_url", "source",
    }
    missing = required - cols
    if missing:
        raise SystemExit(f"leads table missing required columns: {sorted(missing)}")

    sql = """
        SELECT lead_id, company_name, category, area, website_url,
               legacy_email, legacy_form_url
        FROM leads
        WHERE source = ?
        ORDER BY lead_id
    """
    params: list[object] = [_SOURCE]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def main() -> None:
    global _SOURCE

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source")
    known, remaining = parser.parse_known_args()
    _SOURCE = known.source

    base.load_rows = load_rows_by_source
    sys.argv = [sys.argv[0], *remaining]
    base.main()


if __name__ == "__main__":
    main()

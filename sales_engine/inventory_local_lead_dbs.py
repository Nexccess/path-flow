from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_NAMES = {"sales_engine.db", "leads_database.db"}


def safe_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None


def inspect_db(path: Path) -> dict:
    result = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "size_mb": round(path.stat().st_size / 1024 / 1024, 3),
        "tables": [],
        "likely_lead_tables": [],
        "error": None,
    }
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            for table in tables:
                count = safe_count(conn, table)
                cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
                entry = {"table": table, "rows": count, "columns": cols}
                result["tables"].append(entry)
                low = {c.lower() for c in cols}
                if table.lower() in {"leads", "lead", "stores"} or {
                    "company_name",
                    "store_name",
                    "place_id",
                    "google_place_id",
                    "website_url",
                    "form_url",
                    "email",
                } & low:
                    result["likely_lead_tables"].append(entry)
        finally:
            conn.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only inventory of local SQLite lead databases.")
    p.add_argument("--root", type=Path, default=Path.home())
    p.add_argument("--include-all-db", action="store_true")
    args = p.parse_args()

    candidates: list[Path] = []
    for path in args.root.rglob("*.db"):
        if not path.is_file():
            continue
        if args.include_all_db or path.name.lower() in DEFAULT_NAMES or "lead" in str(path).lower():
            candidates.append(path)

    rows = []
    for path in sorted(candidates, key=lambda p: p.stat().st_size, reverse=True):
        rows.append(inspect_db(path))

    print(json.dumps({"root": str(args.root), "database_count": len(rows), "databases": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

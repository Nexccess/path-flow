from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def norm(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def top_counts(rows, key, limit=20):
    c = Counter(norm(r[key]) or "(blank)" for r in rows)
    return [{"value": k, "count": v} for k, v in c.most_common(limit)]


def scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def table_exists(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only profile of lead_intelligence.db population and processing coverage.")
    p.add_argument("--db", type=Path, required=True)
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    uri = f"file:{args.db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "leads"):
            raise SystemExit("leads table not found")

        rows = conn.execute("SELECT * FROM leads ORDER BY lead_id").fetchall()
        total = len(rows)

        place_ids = [norm(r["place_id"]) for r in rows]
        nonblank_place_ids = [x for x in place_ids if x]
        duplicate_place_ids = total - len(set(nonblank_place_ids)) - (total - len(nonblank_place_ids))

        def present(col):
            return sum(1 for r in rows if col in r.keys() and norm(r[col]))

        signals = {
            "website_url": present("website_url"),
            "legacy_form_url": present("legacy_form_url"),
            "legacy_email": present("legacy_email"),
            "legacy_pathflow_url": present("legacy_pathflow_url"),
            "rating": present("rating"),
            "user_ratings_total": present("user_ratings_total"),
            "phone": present("phone"),
            "place_id": len(nonblank_place_ids),
        }

        coverage = {}
        for table in ["lead_enrichment", "lead_contactability", "lead_qualification", "lead_scoring", "lead_supply", "sales_queue"]:
            if table_exists(conn, table):
                rows_count = int(scalar(conn, f'SELECT COUNT(*) FROM "{table}"') or 0)
                distinct_count = None
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                if "lead_id" in cols:
                    distinct_count = int(scalar(conn, f'SELECT COUNT(DISTINCT lead_id) FROM "{table}"') or 0)
                coverage[table] = {
                    "rows": rows_count,
                    "distinct_leads": distinct_count,
                    "coverage_pct": round((distinct_count or 0) / total * 100, 2) if total else 0,
                }

        result = {
            "db": str(args.db),
            "total_leads": total,
            "distinct_place_ids": len(set(nonblank_place_ids)),
            "blank_place_ids": total - len(nonblank_place_ids),
            "duplicate_place_id_rows": max(0, duplicate_place_ids),
            "signal_presence": signals,
            "signal_presence_pct": {k: round(v / total * 100, 2) if total else 0 for k, v in signals.items()},
            "top_categories": top_counts(rows, "category", 30),
            "top_areas": top_counts(rows, "area", 30),
            "legacy_status": top_counts(rows, "legacy_status", 30),
            "lifecycle_status": top_counts(rows, "lifecycle_status", 30),
            "sources": top_counts(rows, "source", 30),
            "processing_coverage": coverage,
        }

        if table_exists(conn, "lead_enrichment"):
            result["enrichment_status"] = [
                {"value": r[0] or "(blank)", "count": r[1]}
                for r in conn.execute("SELECT enrichment_status, COUNT(*) FROM lead_enrichment GROUP BY enrichment_status ORDER BY COUNT(*) DESC")
            ]
        if table_exists(conn, "lead_qualification"):
            result["qualification_status"] = [
                {"value": r[0] or "(blank)", "count": r[1]}
                for r in conn.execute("SELECT qualification_status, COUNT(*) FROM lead_qualification GROUP BY qualification_status ORDER BY COUNT(*) DESC")
            ]
        if table_exists(conn, "lead_scoring"):
            result["scoring_priority"] = [
                {"value": r[0] or "(blank)", "count": r[1]}
                for r in conn.execute("SELECT priority, COUNT(*) FROM lead_scoring GROUP BY priority ORDER BY COUNT(*) DESC")
            ]

        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

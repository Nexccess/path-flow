from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"


def load_csv_store_ids(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "店舗ID" not in (reader.fieldnames or []):
            raise SystemExit("CSV missing 店舗ID")
        return [str(row["店舗ID"]).strip() for row in reader if str(row.get("店舗ID", "")).strip()]


def main() -> None:
    p = argparse.ArgumentParser(description="Diagnose the historical 113-lead qualification baseline without modifying the DB.")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--csv", type=Path, required=True)
    args = p.parse_args()

    ids = load_csv_store_ids(args.csv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        optional = [c for c in ["google_rating", "google_review_count", "qualification_grade", "lead_score", "lead_priority"] if c in cols]
        select = [
            "store_id", "store_name", "contact_status", "screening_status", "send_allowed",
            "email", "contact_form_url", "line_url", "instagram_url", "sales_status", "lp_url"
        ] + optional
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT {','.join(select)} FROM leads WHERE campaign_id=? AND store_id IN ({placeholders}) ORDER BY store_id",
            [CAMPAIGN_ID, *ids],
        ).fetchall()

        found = {str(r["store_id"]) for r in rows}
        missing = [x for x in ids if x not in found]
        extras = conn.execute(
            f"SELECT store_id, store_name FROM leads WHERE campaign_id=? AND store_id NOT IN ({placeholders}) ORDER BY store_id",
            [CAMPAIGN_ID, *ids],
        ).fetchall()

        contact = Counter((r["contact_status"] or "NULL") for r in rows)
        screening = Counter((r["screening_status"] or "NULL") for r in rows)
        sendable = [r for r in rows if bool(r["send_allowed"])]
        manual_reachable = [
            r for r in rows
            if not bool(r["send_allowed"])
            and (r["line_url"] or r["instagram_url"] or (r["contact_status"] or "") in {"READY_LINE", "READY_INSTAGRAM", "READY_SMS", "MANUAL_CHECK", "DISCOVERY_REQUIRED"})
        ]

        result = {
            "csv_ids": len(ids),
            "db_rows_in_csv_scope": len(rows),
            "missing_csv_ids": missing,
            "campaign_extras": [{"store_id": r[0], "store_name": r[1]} for r in extras],
            "contact_status": dict(contact),
            "screening_status": dict(screening),
            "auto_sendable": len(sendable),
            "manual_reachable": len(manual_reachable),
            "auto_sendable_rows": [],
        }
        for r in sendable:
            item = {k: r[k] for k in r.keys()}
            item["has_email"] = bool(r["email"])
            item["has_form"] = bool(r["contact_form_url"])
            item["has_line"] = bool(r["line_url"])
            item["has_instagram"] = bool(r["instagram_url"])
            result["auto_sendable_rows"].append(item)

        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"

CHANNEL_MAP = {
    "email": ("READY_EMAIL", "email"),
    "form": ("READY_FORM", "contact_form_url"),
    "line": ("READY_LINE", "line_url"),
    "instagram": ("READY_INSTAGRAM", "instagram_url"),
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--csv", type=Path, default=Path(__file__).with_name("discovery_seed.csv"))
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    applied = 0
    source_only = 0
    try:
        with args.csv.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        for row in rows:
            store_id = (row.get("store_id") or "").strip()
            source_url = (row.get("source_url") or "").strip() or None
            channel = (row.get("channel") or "").strip().lower()
            value = (row.get("contact_value") or "").strip() or None
            if not store_id:
                continue

            if channel in CHANNEL_MAP and value:
                status, field = CHANNEL_MAP[channel]
                conn.execute(
                    f"""
                    UPDATE leads
                    SET contact_status=?, primary_channel=?, {field}=?,
                        contact_source_url=COALESCE(?, contact_source_url),
                        contact_confidence='HIGH', screening_status=?, updated_at=CURRENT_TIMESTAMP
                    WHERE campaign_id=? AND store_id=?
                    """,
                    (
                        status,
                        channel,
                        value,
                        source_url,
                        "READY" if channel == "email" else "REVIEW",
                        CAMPAIGN_ID,
                        store_id,
                    ),
                )
                applied += 1
            elif source_url:
                conn.execute(
                    """
                    UPDATE leads
                    SET contact_source_url=?, contact_status='DISCOVERY_REQUIRED',
                        primary_channel=NULL, contact_confidence='MEDIUM',
                        screening_status='REVIEW', updated_at=CURRENT_TIMESTAMP
                    WHERE campaign_id=? AND store_id=?
                    """,
                    (source_url, CAMPAIGN_ID, store_id),
                )
                source_only += 1

        conn.commit()
        print(f"seed_applied={applied} source_only={source_only} total_seed={len(rows)}")
        for status, count in conn.execute(
            "SELECT contact_status, COUNT(*) FROM leads WHERE campaign_id=? GROUP BY contact_status ORDER BY contact_status",
            (CAMPAIGN_ID,),
        ):
            print(f"{status}={count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"
CAMPAIGN_NAME = "Path-Flow Nail Campaign #001 / 113 stores"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", type=Path)
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--schema", type=Path, default=Path(__file__).with_name("schema.sql"))
    return p.parse_args()


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(args.schema.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT OR IGNORE INTO campaigns
              (campaign_id, name, industry, status)
            VALUES (?, ?, ?, 'DRAFT')
            """,
            (CAMPAIGN_ID, CAMPAIGN_NAME, "ネイルサロン"),
        )

        with args.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        for row in rows:
            conn.execute(
                """
                INSERT INTO leads (
                  campaign_id, store_id, store_name, area, industry, address, phone,
                  store_url, lp_url, screening_status, sales_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'READY')
                ON CONFLICT(campaign_id, store_id) DO UPDATE SET
                  store_name=excluded.store_name,
                  area=excluded.area,
                  industry=excluded.industry,
                  address=excluded.address,
                  phone=excluded.phone,
                  store_url=excluded.store_url,
                  lp_url=excluded.lp_url,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    CAMPAIGN_ID,
                    clean(row.get("店舗ID")),
                    clean(row.get("店舗名")),
                    clean(row.get("地域")),
                    clean(row.get("業種")),
                    clean(row.get("住所")),
                    clean(row.get("電話番号")),
                    clean(row.get("店舗案内URL")),
                    clean(row.get("Path-Flow_LP_URL")),
                ),
            )

        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE campaign_id=?", (CAMPAIGN_ID,)
        ).fetchone()[0]
        print(f"campaign={CAMPAIGN_ID} leads={count} db={args.db}")
        if count != 113:
            raise SystemExit(f"Expected 113 leads, got {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

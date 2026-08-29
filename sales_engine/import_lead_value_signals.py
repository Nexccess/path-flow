from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from lead_supply_engine import migrate_columns


def normalize(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_float(value: str | None) -> float | None:
    value = normalize(value)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    value = normalize(value)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def run(csv_path: Path, db: Path, campaign_id: str, apply: bool = False) -> dict:
    conn = sqlite3.connect(db)
    try:
        migrate_columns(conn)

        by_place_id = {
            normalize(place_id): store_id
            for store_id, place_id in conn.execute(
                "SELECT store_id, google_place_id FROM leads WHERE campaign_id=?",
                (campaign_id,),
            ).fetchall()
            if normalize(place_id)
        }
        by_store_id = {
            normalize(store_id): store_id
            for (store_id,) in conn.execute(
                "SELECT store_id FROM leads WHERE campaign_id=?",
                (campaign_id,),
            ).fetchall()
            if normalize(store_id)
        }

        matched = 0
        updated = 0
        missing = 0
        invalid_value_rows = 0

        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"店舗ID", "Google評価", "評価件数", "Google_Place_ID"}
            missing_headers = required - set(reader.fieldnames or [])
            if missing_headers:
                raise SystemExit(f"CSV missing required headers: {sorted(missing_headers)}")

            for row in reader:
                place_id = normalize(row.get("Google_Place_ID"))
                source_store_id = normalize(row.get("店舗ID"))
                target_store_id = None

                if place_id and place_id in by_place_id:
                    target_store_id = by_place_id[place_id]
                elif source_store_id and source_store_id in by_store_id:
                    target_store_id = by_store_id[source_store_id]

                if not target_store_id:
                    missing += 1
                    continue

                matched += 1
                rating = parse_float(row.get("Google評価"))
                review_count = parse_int(row.get("評価件数"))
                if rating is None and review_count is None:
                    invalid_value_rows += 1
                    continue

                print(
                    f"MATCH\t{target_store_id}\trating={rating if rating is not None else '-'}"
                    f"\treviews={review_count if review_count is not None else '-'}"
                )

                if apply:
                    conn.execute(
                        """
                        UPDATE leads
                        SET google_rating=COALESCE(?, google_rating),
                            google_review_count=COALESCE(?, google_review_count)
                        WHERE campaign_id=? AND store_id=?
                        """,
                        (rating, review_count, campaign_id, target_store_id),
                    )
                    updated += 1

        if apply:
            conn.commit()

        summary = {
            "campaign_id": campaign_id,
            "matched": matched,
            "updated": updated if apply else 0,
            "missing": missing,
            "invalid_value_rows": invalid_value_rows,
            "applied": apply,
        }
        print(summary)
        return summary
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Import Google rating/review signals into the Revenue Engine lead ledger.")
    p.add_argument("csv_path", type=Path)
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--campaign-id", default="PF-NAIL-001")
    p.add_argument("--apply", action="store_true", help="Persist updates. Default is dry-run.")
    args = p.parse_args()
    run(args.csv_path, args.db, args.campaign_id, apply=args.apply)


if __name__ == "__main__":
    main()

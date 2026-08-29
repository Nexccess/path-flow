from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from import_lead_value_signals import run as import_signals
from lead_supply_engine import run as run_scoring
from revenue_status import inspect

# v1.2 benchmark for the 113-store CSV scope.
# Derived from verified contactability + explicit value-signal rules:
#   AUTO_SENDABLE = 15
#     HIGH   = rating >= 4.5 and reviews >= 50 => 6
#     MEDIUM = other auto-sendable => 9
#   MANUAL_REACHABLE => LOW = 71
#   EXCLUDE => 27
EXPECTED = {
    "HIGH": 6,
    "MEDIUM": 9,
    "LOW": 71,
    "EXCLUDE": 27,
}
CAMPAIGN_ID = "PF-NAIL-001"


def csv_store_ids(source_csv: Path) -> set[str]:
    with source_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "店舗ID" not in (reader.fieldnames or []):
            raise SystemExit("CSV missing 店舗ID")
        return {str(row["店舗ID"]).strip() for row in reader if str(row.get("店舗ID", "")).strip()}


def restrict_work_db_to_ids(work_db: Path, ids: set[str]) -> list[dict[str, str]]:
    """Remove campaign extras from the temporary copy only.

    This makes validate_nail_113.py a true 113-row benchmark while leaving the
    source DB untouched. Extra campaign rows are returned for audit visibility.
    """
    conn = sqlite3.connect(work_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT store_id, store_name FROM leads WHERE campaign_id=? ORDER BY store_id",
            (CAMPAIGN_ID,),
        ).fetchall()
        extras = [
            {"store_id": str(r["store_id"]), "store_name": str(r["store_name"])}
            for r in rows
            if str(r["store_id"]) not in ids
        ]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM leads WHERE campaign_id=? AND store_id NOT IN ({placeholders})",
                [CAMPAIGN_ID, *sorted(ids)],
            )
        else:
            conn.execute("DELETE FROM leads WHERE campaign_id=?", (CAMPAIGN_ID,))
        conn.commit()
        return extras
    finally:
        conn.close()


def validate(source_db: Path, source_csv: Path) -> dict:
    if not source_db.exists():
        raise SystemExit(f"DB not found: {source_db}")
    if not source_csv.exists():
        raise SystemExit(f"CSV not found: {source_csv}")

    ids = csv_store_ids(source_csv)
    benchmark_total = sum(EXPECTED.values())
    benchmark_scope_valid = benchmark_total == len(ids)

    with tempfile.TemporaryDirectory(prefix="revenue_validate_") as tmp:
        work_db = Path(tmp) / source_db.name
        shutil.copy2(source_db, work_db)

        campaign_extras = restrict_work_db_to_ids(work_db, ids)

        signal_summary = import_signals(
            csv_path=source_csv,
            db=work_db,
            campaign_id=CAMPAIGN_ID,
            apply=True,
        )
        scoring_summary = run_scoring(
            db=work_db,
            campaign_id=CAMPAIGN_ID,
            apply=True,
        )
        status = inspect(work_db, CAMPAIGN_ID)

        actual_priority = scoring_summary["priority"]
        comparison = {
            key: {
                "expected": expected,
                "actual": actual_priority.get(key, 0),
                "delta": actual_priority.get(key, 0) - expected,
            }
            for key, expected in EXPECTED.items()
        }
        exact_match = benchmark_scope_valid and all(item["delta"] == 0 for item in comparison.values())

        return {
            "campaign_id": CAMPAIGN_ID,
            "source_db_unchanged": True,
            "csv_scope_count": len(ids),
            "campaign_extras_excluded_from_benchmark": campaign_extras,
            "benchmark_total": benchmark_total,
            "benchmark_scope_valid": benchmark_scope_valid,
            "signals": signal_summary,
            "scoring": scoring_summary,
            "expected": EXPECTED,
            "comparison": comparison,
            "exact_match": exact_match,
            "status": status,
        }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Validate PF-NAIL-001 v1.2 scoring on exactly the 113-store CSV scope without modifying the source DB."
    )
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument(
        "--require-exact-match",
        action="store_true",
        help="Exit non-zero unless the 113-row v1.2 benchmark matches exactly.",
    )
    args = p.parse_args()

    result = validate(args.db, args.csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.require_exact_match and not result["exact_match"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
from pathlib import Path

from import_lead_value_signals import run as import_signals
from lead_supply_engine import run as run_scoring
from revenue_status import inspect

EXPECTED = {
    "HIGH": 4,
    "MEDIUM": 12,
    "LOW": 21,
    "EXCLUDE": 77,
}
CAMPAIGN_ID = "PF-NAIL-001"


def csv_store_ids(source_csv: Path) -> set[str]:
    with source_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "店舗ID" not in (reader.fieldnames or []):
            raise SystemExit("CSV missing 店舗ID")
        return {str(row["店舗ID"]).strip() for row in reader if str(row.get("店舗ID", "")).strip()}


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
            "benchmark_total": benchmark_total,
            "benchmark_scope_valid": benchmark_scope_valid,
            "benchmark_warning": None if benchmark_scope_valid else (
                f"Expected distribution totals {benchmark_total}, but CSV scope contains {len(ids)} stores. "
                "Do not treat exact-match comparison as valid until benchmark scope is reconciled."
            ),
            "signals": signal_summary,
            "scoring": scoring_summary,
            "expected": EXPECTED,
            "comparison": comparison,
            "exact_match": exact_match,
            "status": status,
        }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Validate PF-NAIL-001 qualification/scoring without modifying the source DB."
    )
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument(
        "--require-exact-match",
        action="store_true",
        help="Exit non-zero unless benchmark scope is valid and priority distribution matches exactly.",
    )
    args = p.parse_args()

    result = validate(args.db, args.csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.require_exact_match and not result["exact_match"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

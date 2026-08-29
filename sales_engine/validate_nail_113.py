from __future__ import annotations

import argparse
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


def validate(source_db: Path, source_csv: Path) -> dict:
    if not source_db.exists():
        raise SystemExit(f"DB not found: {source_db}")
    if not source_csv.exists():
        raise SystemExit(f"CSV not found: {source_csv}")

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
        exact_match = all(item["delta"] == 0 for item in comparison.values())

        return {
            "campaign_id": CAMPAIGN_ID,
            "source_db_unchanged": True,
            "signals": signal_summary,
            "scoring": scoring_summary,
            "expected": EXPECTED,
            "comparison": comparison,
            "exact_match": exact_match,
            "status": status,
        }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Validate PF-NAIL-001 qualification/scoring against the 113-store benchmark without modifying the source DB."
    )
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument(
        "--require-exact-match",
        action="store_true",
        help="Exit non-zero unless HIGH/MEDIUM/LOW/EXCLUDE exactly matches the validated benchmark.",
    )
    args = p.parse_args()

    result = validate(args.db, args.csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.require_exact_match and not result["exact_match"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

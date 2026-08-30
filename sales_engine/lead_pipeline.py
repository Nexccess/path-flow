from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SOURCE = "lead-discovery-v1"
HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parent / "lead_intelligence" / "data" / "working" / "lead_intelligence.db"


def max_lead_id(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT COALESCE(MAX(lead_id), 0) FROM leads").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def run(cmd: list[str]) -> None:
    print("\n>>> " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=HERE, check=True)


def summary(db: Path, min_lead_id: int, campaign_id: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        new_rows = conn.execute(
            """
            SELECT lead_id, company_name, website_url
            FROM leads
            WHERE source=? AND lead_id>=?
            ORDER BY lead_id
            """,
            (SOURCE, min_lead_id),
        ).fetchall()

        decisions = conn.execute(
            """
            SELECT d.decision, d.reason_code
            FROM screening_decisions d
            JOIN leads l ON l.lead_id=d.lead_id
            WHERE l.source=? AND l.lead_id>=?
            """,
            (SOURCE, min_lead_id),
        ).fetchall()

        decision_counts = Counter(r[0] for r in decisions)
        reason_counts = Counter(r[1] for r in decisions)
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM sales_queue WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]

        return {
            "campaign_id": campaign_id,
            "new_leads": len(new_rows),
            "go": decision_counts.get("GO", 0),
            "hold": decision_counts.get("HOLD", 0),
            "close": decision_counts.get("CLOSE", 0),
            "sales_queue_added": int(queue_count),
            "reason_code": dict(reason_counts.most_common()),
            "new_lead_ids": [r[0] for r in new_rows],
        }
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="One-command Lead Discovery -> Screening -> Sales Queue pipeline")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--area", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--max-results", type=int, default=30)
    p.add_argument("--campaign-id")
    args = p.parse_args()

    db = args.db.resolve()
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")

    campaign_id = args.campaign_id or f"WEB-DISCOVERY-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    queries = args.query or [
        f"{args.area} {args.category} 公式",
        f"{args.area} {args.category} お問い合わせ",
        f"{args.area} {args.category} 店舗",
    ]

    before = max_lead_id(db)
    min_new_id = before + 1

    print("=" * 68)
    print("Nexccess Lead Pipeline")
    print(f"campaign : {campaign_id}")
    print(f"area     : {args.area}")
    print(f"category : {args.category}")
    print(f"start ID : {min_new_id}")
    print("=" * 68, flush=True)

    discovery_cmd = [
        sys.executable,
        str(HERE / "lead_discovery_runtime.py"),
        "--db", str(db),
        "--area", args.area,
        "--category", args.category,
        "--max-results", str(args.max_results),
        "--apply",
    ]
    for q in queries:
        discovery_cmd.extend(["--query", q])

    run(discovery_cmd)

    after = max_lead_id(db)
    if after < min_new_id:
        report = {
            "campaign_id": campaign_id,
            "new_leads": 0,
            "go": 0,
            "hold": 0,
            "close": 0,
            "sales_queue_added": 0,
            "reason_code": {},
            "message": "No new leads discovered; screening skipped.",
        }
        print("\n=== PIPELINE RESULT ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    screening_cmd = [
        sys.executable,
        str(HERE / "screening_discovery_runtime.py"),
        "--db", str(db),
        "--campaign-id", campaign_id,
        "--source", SOURCE,
        "--min-lead-id", str(min_new_id),
        "--apply",
    ]
    run(screening_cmd)

    report = summary(db, min_new_id, campaign_id)
    print("\n=== PIPELINE RESULT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

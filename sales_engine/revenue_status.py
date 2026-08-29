from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_CAMPAIGN_ID = "PF-NAIL-001"


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def inspect(db: Path, campaign_id: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        lead_columns = table_columns(conn, "leads")
        total = count(conn, "SELECT COUNT(*) FROM leads WHERE campaign_id=?", (campaign_id,))

        result: dict = {
            "campaign_id": campaign_id,
            "total_leads": total,
            "stages": {},
        }

        contact_cols = {"contact_status", "screening_status", "send_allowed"}
        if contact_cols.issubset(lead_columns):
            contactability = count(
                conn,
                """
                SELECT COUNT(*) FROM leads
                WHERE campaign_id=?
                  AND contact_status IS NOT NULL
                  AND contact_status NOT IN ('PENDING', 'LEGACY_NO_CONTACT')
                """,
                (campaign_id,),
            )
            auto_sendable = count(
                conn,
                "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND send_allowed=1",
                (campaign_id,),
            )
            result["stages"]["contactability"] = {
                "status": "READY" if contactability == total and total else "PARTIAL",
                "processed": contactability,
                "auto_sendable": auto_sendable,
            }
        else:
            result["stages"]["contactability"] = {"status": "NOT_IMPLEMENTED"}

        scoring_cols = {"qualification_grade", "lead_score", "lead_priority"}
        if scoring_cols.issubset(lead_columns):
            scored = count(
                conn,
                "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND lead_priority IS NOT NULL",
                (campaign_id,),
            )
            priority = {
                name: count(
                    conn,
                    "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND lead_priority=?",
                    (campaign_id, name),
                )
                for name in ("HOT", "HIGH", "MEDIUM", "LOW", "EXCLUDE")
            }
            grade = {
                name: count(
                    conn,
                    "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND qualification_grade=?",
                    (campaign_id, name),
                )
                for name in ("A", "B", "C", "D")
            }
            result["stages"]["qualification_scoring"] = {
                "status": "READY" if scored == total and total else "PARTIAL",
                "processed": scored,
                "priority": priority,
                "grade": grade,
            }
        else:
            result["stages"]["qualification_scoring"] = {"status": "NOT_IMPLEMENTED"}

        if "lp_url" in lead_columns:
            lp_ready = count(
                conn,
                "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND lp_url IS NOT NULL AND TRIM(lp_url)<>''",
                (campaign_id,),
            )
            result["stages"]["lp_production"] = {
                "status": "READY" if lp_ready == total and total else "PARTIAL",
                "processed": lp_ready,
            }

        if "sales_status" in lead_columns:
            sales = {
                status: count(
                    conn,
                    "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND sales_status=?",
                    (campaign_id, status),
                )
                for status in (
                    "READY", "SENT", "FOLLOWUP_1", "FOLLOWUP_2",
                    "HUMAN_ACTION", "CLOSED_NO_RESPONSE", "WON_ONE_TIME",
                    "WON_MAINTENANCE", "LOST",
                )
            }
            result["stages"]["sales_execution"] = {
                "status": "IMPLEMENTED",
                "states": sales,
            }

        blocking: list[str] = []
        if total == 0:
            blocking.append("campaign_has_no_leads")
        elif result["stages"].get("qualification_scoring", {}).get("status") != "READY":
            blocking.append("qualification_scoring_not_complete")
        elif result["stages"].get("lp_production", {}).get("status") != "READY":
            blocking.append("lp_production_not_complete")

        result["blocking_items"] = blocking
        result["next_action"] = (
            "RUN_QUALIFICATION_SCORING"
            if "qualification_scoring_not_complete" in blocking
            else "COMPLETE_LP_PRODUCTION"
            if "lp_production_not_complete" in blocking
            else "RUN_SALES_EXECUTION"
            if not blocking
            else "LOAD_LEADS"
        )
        return result
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Machine-readable Revenue Engine progress report.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    args = p.parse_args()
    print(json.dumps(inspect(args.db, args.campaign_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

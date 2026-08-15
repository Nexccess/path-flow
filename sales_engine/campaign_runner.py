from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from engine import close_no_response, due_actions, mark_sent
from graph_mail import GraphConfig, GraphMailClient
from templates import render

CAMPAIGN_ID = "PF-NAIL-001"


def run(db: Path, live: bool = False, max_sends: int | None = None) -> dict:
    conn = sqlite3.connect(db)
    client = GraphMailClient(GraphConfig.from_env()) if live else None
    counts = {"initial": 0, "followup1": 0, "followup2": 0, "close": 0, "skipped": 0}
    try:
        actions = due_actions(conn)
        sent_count = 0
        for store_id, store_name, stage in actions:
            if stage == "close":
                close_no_response(conn, store_id)
                conn.commit()
                counts["close"] += 1
                continue

            if max_sends is not None and sent_count >= max_sends:
                break

            row = conn.execute(
                """
                SELECT lp_url, email, contact_status, send_allowed, human_action, sales_status
                FROM leads WHERE campaign_id=? AND store_id=?
                """,
                (CAMPAIGN_ID, store_id),
            ).fetchone()
            if not row:
                counts["skipped"] += 1
                continue
            lp_url, email, contact_status, send_allowed, human_action, sales_status = row
            if human_action or not email or contact_status != "READY_EMAIL" or not send_allowed:
                counts["skipped"] += 1
                continue

            subject, body = render(stage, store_name, store_id, lp_url)
            if live:
                assert client is not None
                client.send_text(email, subject, body, dry_run=False)
            else:
                print(f"DRY-RUN\t{stage}\t{store_id}\t{store_name}\t{email}\t{subject}")

            mark_sent(conn, store_id, stage)
            conn.commit()
            counts[stage] += 1
            sent_count += 1
        return counts
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--live", action="store_true", help="Actually send mail. Omit for dry-run.")
    p.add_argument("--max-sends", type=int)
    args = p.parse_args()
    result = run(args.db, live=args.live, max_sends=args.max_sends)
    print(result)


if __name__ == "__main__":
    main()

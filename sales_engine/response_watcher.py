from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import mark_response
from graph_mail import GraphConfig, GraphMailClient, extract_store_id, sender_address

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))


def find_store_by_sender(conn: sqlite3.Connection, sender: str | None) -> str | None:
    if not sender:
        return None
    row = conn.execute(
        """
        SELECT store_id FROM leads
        WHERE campaign_id=? AND lower(email)=lower(?)
        ORDER BY updated_at DESC LIMIT 1
        """,
        (CAMPAIGN_ID, sender),
    ).fetchone()
    return row[0] if row else None


def run(db: Path, lookback_hours: int = 72) -> tuple[int, int]:
    client = GraphMailClient(GraphConfig.from_env())
    since = datetime.now(JST) - timedelta(hours=lookback_hours)
    messages = client.recent_inbox(since, top=200)
    conn = sqlite3.connect(db)
    matched = 0
    unmatched = 0
    try:
        for msg in messages:
            message_id = msg.get("id") or msg.get("internetMessageId")
            if not message_id:
                continue
            already = conn.execute(
                "SELECT 1 FROM events WHERE event_type='RESPONSE_RECEIVED' AND external_message_id=? LIMIT 1",
                (message_id,),
            ).fetchone()
            if already:
                continue

            store_id = extract_store_id(msg.get("subject"))
            if not store_id:
                store_id = find_store_by_sender(conn, sender_address(msg))
            if not store_id:
                unmatched += 1
                continue

            mark_response(conn, store_id, response_type="UNKNOWN", external_message_id=message_id)
            conn.commit()
            matched += 1
        return matched, unmatched
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--lookback-hours", type=int, default=72)
    args = p.parse_args()
    matched, unmatched = run(args.db, args.lookback_hours)
    print(f"matched_responses={matched} unmatched_messages={unmatched}")


if __name__ == "__main__":
    main()

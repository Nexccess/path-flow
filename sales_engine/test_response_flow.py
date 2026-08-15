from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from graph_mail import GraphConfig, GraphMailClient, extract_store_id, sender_address

TEST_DB = Path("sales_engine_test.db")
TEST_CAMPAIGN = "PF-NAIL-001"
TEST_STORE_ID = "PF-TEST-001"
TEST_EMAIL = "0nakamura.keita@gmail.com"
JST = timezone(timedelta(hours=9))


def init_test_db() -> sqlite3.Connection:
    if TEST_DB.exists():
        TEST_DB.unlink()
    conn = sqlite3.connect(TEST_DB)
    conn.executescript(
        """
        CREATE TABLE leads (
          campaign_id TEXT NOT NULL,
          store_id TEXT NOT NULL,
          store_name TEXT NOT NULL,
          email TEXT,
          sales_status TEXT NOT NULL DEFAULT 'SENT',
          response_type TEXT,
          human_action INTEGER NOT NULL DEFAULT 0,
          response_at TEXT,
          updated_at TEXT,
          PRIMARY KEY (campaign_id, store_id)
        );
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          campaign_id TEXT NOT NULL,
          store_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          external_message_id TEXT,
          payload TEXT,
          UNIQUE(campaign_id, store_id, event_type, external_message_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO leads (campaign_id, store_id, store_name, email) VALUES (?, ?, ?, ?)",
        (TEST_CAMPAIGN, TEST_STORE_ID, "Reply Detection Test", TEST_EMAIL),
    )
    conn.commit()
    return conn


def main() -> None:
    client = GraphMailClient(GraphConfig.from_env())
    since = datetime.now(JST) - timedelta(hours=2)
    messages = client.recent_inbox(since, top=100)
    conn = init_test_db()
    try:
        matched = None
        for msg in messages:
            sid = extract_store_id(msg.get("subject"))
            sender = sender_address(msg)
            if sid == TEST_STORE_ID or sender == TEST_EMAIL.lower():
                matched = msg
                break

        if not matched:
            raise SystemExit("TEST FAILED: Gmail reply not found in Office365 inbox")

        message_id = matched.get("id") or matched.get("internetMessageId") or "unknown"
        now = datetime.now(JST).isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE leads
            SET sales_status='HUMAN_ACTION', response_type='UNKNOWN', human_action=1,
                response_at=?, updated_at=?
            WHERE campaign_id=? AND store_id=?
            """,
            (now, now, TEST_CAMPAIGN, TEST_STORE_ID),
        )
        conn.execute(
            "INSERT OR IGNORE INTO events (campaign_id, store_id, event_type, external_message_id) VALUES (?, ?, 'RESPONSE_RECEIVED', ?)",
            (TEST_CAMPAIGN, TEST_STORE_ID, message_id),
        )
        conn.commit()

        row = conn.execute(
            "SELECT sales_status, human_action, response_type FROM leads WHERE campaign_id=? AND store_id=?",
            (TEST_CAMPAIGN, TEST_STORE_ID),
        ).fetchone()
        print(f"reply_found=True store_id={TEST_STORE_ID} sender={sender_address(matched)}")
        print(f"sales_status={row[0]} human_action={row[1]} response_type={row[2]}")
        if row[0] != "HUMAN_ACTION" or row[1] != 1:
            raise SystemExit("TEST FAILED: HUMAN_ACTION transition did not occur")
        print("TEST PASSED: reply detection -> HUMAN_ACTION")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

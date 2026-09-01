from __future__ import annotations

import sqlite3
import unittest

from engine import due_actions
from response_watcher import apply_classification


CAMPAIGN_ID = "PF-HAIR-YOKOHAMA-001"
STORE_ID = "9"


class SalesReadyResponseGateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE leads (
              campaign_id TEXT NOT NULL,
              store_id TEXT NOT NULL,
              store_name TEXT NOT NULL,
              email TEXT,
              contact_status TEXT NOT NULL DEFAULT 'READY_EMAIL',
              sales_status TEXT NOT NULL DEFAULT 'SENT',
              response_type TEXT,
              human_action INTEGER NOT NULL DEFAULT 0,
              initial_sent_at TEXT,
              followup1_sent_at TEXT,
              followup2_sent_at TEXT,
              response_at TEXT,
              closed_at TEXT,
              close_reason TEXT,
              updated_at TEXT,
              PRIMARY KEY (campaign_id, store_id)
            );
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              campaign_id TEXT NOT NULL,
              store_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              event_at TEXT NOT NULL,
              external_message_id TEXT,
              payload TEXT,
              UNIQUE(campaign_id, store_id, event_type, external_message_id)
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO leads (
              campaign_id, store_id, store_name, email,
              contact_status, sales_status, initial_sent_at
            ) VALUES (?, ?, ?, ?, 'READY_EMAIL', 'SENT', '2026-08-20T10:00:00+09:00')
            """,
            (CAMPAIGN_ID, STORE_ID, "Violet Yokohama", "info@violet.tokyo"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_interested_reply_moves_to_human_action(self):
        apply_classification(
            self.conn,
            STORE_ID,
            {
                "label": "INTERESTED",
                "confidence": 0.95,
                "needs_human": True,
                "reason": "customer wants details",
            },
            "message-1",
            campaign_id=CAMPAIGN_ID,
        )
        self.conn.commit()

        row = self.conn.execute(
            """
            SELECT sales_status, response_type, human_action, response_at
            FROM leads WHERE campaign_id=? AND store_id=?
            """,
            (CAMPAIGN_ID, STORE_ID),
        ).fetchone()
        self.assertEqual(row[0], "HUMAN_ACTION")
        self.assertEqual(row[1], "INTERESTED")
        self.assertEqual(row[2], 1)
        self.assertIsNotNone(row[3])

        event = self.conn.execute(
            """
            SELECT event_type FROM events
            WHERE campaign_id=? AND store_id=? AND external_message_id=?
            ORDER BY id
            """,
            (CAMPAIGN_ID, STORE_ID, "message-1"),
        ).fetchall()
        self.assertIn(("RESPONSE_RECEIVED",), event)

    def test_human_action_is_never_due_for_followup(self):
        apply_classification(
            self.conn,
            STORE_ID,
            {
                "label": "PRICE_INQUIRY",
                "confidence": 0.9,
                "needs_human": True,
            },
            "message-2",
            campaign_id=CAMPAIGN_ID,
        )
        self.conn.commit()

        actions = due_actions(self.conn, campaign_id=CAMPAIGN_ID)
        self.assertEqual(actions, [])

    def test_declined_reply_auto_closes_without_human_followup(self):
        apply_classification(
            self.conn,
            STORE_ID,
            {
                "label": "DECLINED",
                "confidence": 0.95,
                "needs_human": False,
            },
            "message-3",
            campaign_id=CAMPAIGN_ID,
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT sales_status, human_action, close_reason FROM leads WHERE campaign_id=? AND store_id=?",
            (CAMPAIGN_ID, STORE_ID),
        ).fetchone()
        self.assertEqual(row, ("LOST", 0, "DECLINED"))
        self.assertEqual(due_actions(self.conn, campaign_id=CAMPAIGN_ID), [])


if __name__ == "__main__":
    unittest.main()

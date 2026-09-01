from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from campaign_runner import JST, reserve_send, within_send_window


CAMPAIGN_ID = "PF-HAIR-YOKOHAMA-001"


class SalesReadySafetyTests(unittest.TestCase):
    def test_send_window_allows_weekday_business_hours(self):
        # 2026-09-02 is Wednesday.
        now = datetime(2026, 9, 2, 10, 0, tzinfo=JST)
        self.assertTrue(within_send_window(now))

    def test_send_window_blocks_weekend(self):
        # 2026-09-05 is Saturday.
        now = datetime(2026, 9, 5, 12, 0, tzinfo=JST)
        self.assertFalse(within_send_window(now))

    def test_send_window_blocks_outside_business_hours(self):
        before = datetime(2026, 9, 2, 9, 59, tzinfo=JST)
        after = datetime(2026, 9, 2, 19, 1, tzinfo=JST)
        self.assertFalse(within_send_window(before))
        self.assertFalse(within_send_window(after))

    def test_reserve_send_is_single_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sales_engine.db"
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE leads (
                      campaign_id TEXT NOT NULL,
                      store_id TEXT NOT NULL,
                      sales_status TEXT NOT NULL,
                      human_action INTEGER NOT NULL DEFAULT 0,
                      updated_at TEXT,
                      PRIMARY KEY (campaign_id, store_id)
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO leads (campaign_id, store_id, sales_status, human_action) VALUES (?, ?, 'READY', 0)",
                    (CAMPAIGN_ID, "9"),
                )
                conn.commit()

                self.assertTrue(reserve_send(conn, "9", "initial", CAMPAIGN_ID))
                self.assertFalse(reserve_send(conn, "9", "initial", CAMPAIGN_ID))
                status = conn.execute(
                    "SELECT sales_status FROM leads WHERE campaign_id=? AND store_id='9'",
                    (CAMPAIGN_ID,),
                ).fetchone()[0]
                self.assertEqual(status, "SENDING_INITIAL")
            finally:
                conn.close()

    def test_reserve_send_blocks_human_action(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE leads (
                  campaign_id TEXT NOT NULL,
                  store_id TEXT NOT NULL,
                  sales_status TEXT NOT NULL,
                  human_action INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT,
                  PRIMARY KEY (campaign_id, store_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO leads (campaign_id, store_id, sales_status, human_action) VALUES (?, ?, 'READY', 1)",
                (CAMPAIGN_ID, "9"),
            )
            conn.commit()
            self.assertFalse(reserve_send(conn, "9", "initial", CAMPAIGN_ID))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

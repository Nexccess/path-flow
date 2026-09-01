from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from campaign_runner import CAMPAIGN_ID, campaign_is_sales_ready, run


TEST_CAMPAIGN = "PF-HAIR-YOKOHAMA-001"


class SalesReadyCampaignGateTests(unittest.TestCase):
    def test_campaign_status_must_be_sales_ready(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            conn.execute("INSERT INTO campaigns VALUES (?, 'DRAFT')", (TEST_CAMPAIGN,))
            self.assertFalse(campaign_is_sales_ready(conn, TEST_CAMPAIGN))
            conn.execute("UPDATE campaigns SET status='SALES_READY' WHERE campaign_id=?", (TEST_CAMPAIGN,))
            self.assertTrue(campaign_is_sales_ready(conn, TEST_CAMPAIGN))
        finally:
            conn.close()

    def test_missing_campaigns_table_is_not_ready(self):
        conn = sqlite3.connect(":memory:")
        try:
            self.assertFalse(campaign_is_sales_ready(conn, TEST_CAMPAIGN))
        finally:
            conn.close()

    def test_live_requires_explicit_batch_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sales_engine.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            conn.execute("INSERT INTO campaigns VALUES (?, 'SALES_READY')", (TEST_CAMPAIGN,))
            conn.commit()
            conn.close()

            result = run(db, live=True, max_sends=None, campaign_id=TEST_CAMPAIGN)
            self.assertEqual(result["blocked_live_without_limit"], 1)

    def test_live_blocks_campaign_not_sales_ready_before_mail_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sales_engine.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            conn.execute("INSERT INTO campaigns VALUES (?, 'DRAFT')", (TEST_CAMPAIGN,))
            conn.commit()
            conn.close()

            result = run(db, live=True, max_sends=5, campaign_id=TEST_CAMPAIGN)
            self.assertEqual(result["blocked_campaign_not_sales_ready"], 1)


if __name__ == "__main__":
    unittest.main()

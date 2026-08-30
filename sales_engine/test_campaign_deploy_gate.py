from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import campaign_runner


class CampaignDeployGateTest(unittest.TestCase):
    def make_db(self, with_deploy_status: bool, deploy_status=None, lp_url=None) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Path(tmp.name)
        conn = sqlite3.connect(db)
        deploy_col = ", deploy_status TEXT" if with_deploy_status else ""
        conn.execute(
            f"""
            CREATE TABLE leads (
              campaign_id TEXT NOT NULL,
              store_id TEXT NOT NULL,
              store_name TEXT NOT NULL,
              lp_url TEXT,
              email TEXT,
              contact_status TEXT,
              send_allowed INTEGER,
              human_action INTEGER,
              sales_status TEXT,
              initial_sent_at TEXT,
              followup1_sent_at TEXT,
              followup2_sent_at TEXT
              {deploy_col}
            )
            """
        )
        cols = [
            "campaign_id","store_id","store_name","lp_url","email","contact_status",
            "send_allowed","human_action","sales_status","initial_sent_at",
            "followup1_sent_at","followup2_sent_at"
        ]
        vals = [
            campaign_runner.CAMPAIGN_ID,"STORE-1","テストネイル",lp_url,
            "owner@example.jp","READY_EMAIL",1,0,"READY",None,None,None
        ]
        if with_deploy_status:
            cols.append("deploy_status")
            vals.append(deploy_status)
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        conn.close()
        return db

    def test_blocks_when_deploy_status_column_is_missing(self):
        db = self.make_db(False, lp_url="https://example.com/lp")
        try:
            result = campaign_runner.run(db, live=False)
            self.assertEqual(result["initial"], 0)
            self.assertEqual(result["blocked_deploy_not_ready"], 1)
        finally:
            db.unlink(missing_ok=True)

    def test_blocks_when_deploy_not_ready(self):
        db = self.make_db(True, deploy_status="PENDING", lp_url="https://example.com/lp")
        try:
            result = campaign_runner.run(db, live=False)
            self.assertEqual(result["initial"], 0)
            self.assertEqual(result["blocked_deploy_not_ready"], 1)
        finally:
            db.unlink(missing_ok=True)

    def test_allows_dry_run_when_deploy_ready_and_lp_url_present(self):
        db = self.make_db(True, deploy_status="READY", lp_url="https://example.com/lp")
        try:
            result = campaign_runner.run(db, live=False)
            self.assertEqual(result["initial"], 1)
            self.assertEqual(result["blocked_deploy_not_ready"], 0)
        finally:
            db.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

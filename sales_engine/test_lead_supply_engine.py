from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lead_supply_engine import run


SCHEMA = """
CREATE TABLE leads (
  campaign_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  store_name TEXT NOT NULL,
  contact_status TEXT,
  screening_status TEXT,
  send_allowed INTEGER,
  email TEXT,
  contact_form_url TEXT,
  line_url TEXT,
  instagram_url TEXT,
  updated_at TEXT
);
"""


class LeadSupplyEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sales_engine.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.executemany(
            """
            INSERT INTO leads (
              campaign_id, store_id, store_name, contact_status,
              screening_status, send_allowed, email, contact_form_url,
              line_url, instagram_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("PF-NAIL-001", "1", "Email Lead", "READY_EMAIL", "READY", 1,
                 "sales@example.jp", None, None, None, "x"),
                ("PF-NAIL-001", "2", "Form Lead", "READY_FORM", "FORM_AUTO_READY", 1,
                 None, "https://example.jp/contact", None, None, "x"),
                ("PF-NAIL-001", "3", "Line Lead", "READY_LINE", "REVIEW", 0,
                 None, None, "https://line.me/example", None, "x"),
                ("PF-NAIL-001", "4", "Blocked Lead", "READY_FORM", "FORM_BLOCKED_CAPTCHA", 0,
                 None, "https://example.jp/contact", None, None, "x"),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_does_not_persist_scores(self) -> None:
        summary = run(self.db, "PF-NAIL-001", apply=False)
        self.assertEqual(summary["targets"], 4)
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT lead_priority FROM leads").fetchall()
        conn.close()
        self.assertTrue(all(row[0] is None for row in rows))

    def test_apply_persists_expected_qualification(self) -> None:
        summary = run(self.db, "PF-NAIL-001", apply=True)
        self.assertEqual(summary["priority"]["MEDIUM"], 2)
        self.assertEqual(summary["priority"]["LOW"], 1)
        self.assertEqual(summary["priority"]["EXCLUDE"], 1)

        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT store_id, qualification_grade, lead_score, lead_priority FROM leads ORDER BY store_id"
        ).fetchall()
        conn.close()
        self.assertEqual(
            rows,
            [
                ("1", "A", 60, "MEDIUM"),
                ("2", "A", 60, "MEDIUM"),
                ("3", "C", 25, "LOW"),
                ("4", "D", 0, "EXCLUDE"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

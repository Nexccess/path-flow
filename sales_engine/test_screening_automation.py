from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from screening_automation import classify, ensure_schema, persist_decision


class ScreeningAutomationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE leads (
              lead_id INTEGER PRIMARY KEY,
              company_name TEXT,
              category TEXT,
              area TEXT,
              website_url TEXT,
              legacy_email TEXT,
              legacy_form_url TEXT
            )
            """
        )
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, lead_id, website=None, email=None, form=None, category="美容室", area="横浜駅"):
        self.conn.execute(
            "INSERT INTO leads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead_id, f"Lead {lead_id}", category, area, website, email, form),
        )
        self.conn.commit()
        return self.conn.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone()

    def test_go_with_valid_email(self):
        row = self.add(1, website="https://shop.example", email="info@real-shop.jp")
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason, d.route), ("GO", "VALID_EMAIL", "email"))

    def test_go_with_contact_form(self):
        row = self.add(2, website="https://shop.example", form="https://shop.example/contact")
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason, d.route), ("GO", "CONTACT_FORM", "form"))

    def test_hold_booking_form(self):
        row = self.add(3, website="https://shop.example", form="https://shop.example/reserve")
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason), ("HOLD", "AMBIGUOUS_FORM"))

    def test_close_portal_only(self):
        row = self.add(4, website="https://beauty.hotpepper.jp/kr/sln123", form="https://beauty.hotpepper.jp/nail/")
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason), ("CLOSE", "PORTAL_ONLY"))

    def test_invalid_email_with_website_holds_not_closes(self):
        row = self.add(5, website="https://shop.example", email="sample@example.jp")
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason), ("HOLD", "INVALID_EMAIL_RECHECK"))

    def test_no_route_closes(self):
        row = self.add(6)
        d = classify(row, set(), set())
        self.assertEqual((d.status, d.reason), ("CLOSE", "NO_CONTACT_ROUTE"))

    def test_persist_routes_go_hold_close(self):
        rows = [
            self.add(10, website="https://a.example", email="sales@a.jp"),
            self.add(11, website="https://b.example"),
            self.add(12),
        ]
        for row in rows:
            persist_decision(self.conn, row, classify(row, set(), set()), "TEST-CAMPAIGN")
        self.conn.commit()

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sales_queue").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hold_pool").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_audit").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM screening_decisions").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()

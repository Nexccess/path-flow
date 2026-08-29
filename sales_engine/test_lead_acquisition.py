import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lead_acquisition import (
    ContactDiscovery,
    discover_contact,
    existing_place_ids,
    extract_valid_email,
    insert_candidate,
    update_contact,
)


class FakeResponse:
    def __init__(self, text, url="https://example.jp/"):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


class LeadAcquisitionTest(unittest.TestCase):
    def make_db(self):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "lead.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE leads (
              lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
              place_id TEXT UNIQUE,
              company_name TEXT,
              category TEXT,
              area TEXT,
              address TEXT,
              phone TEXT,
              website_url TEXT,
              rating REAL,
              user_ratings_total INTEGER,
              lifecycle_status TEXT,
              source TEXT,
              created_at TEXT
            )
            """
        )
        conn.commit()
        return td, path, conn

    def test_extract_valid_email_skips_known_junk(self):
        html = "sample@example.jp info@real-shop.jp"
        self.assertEqual(extract_valid_email(html), "info@real-shop.jp")

    def test_discover_contact_prefers_contact_over_booking(self):
        html = """
        <a href='/reserve'>ご予約</a>
        <a href='/contact'>お問い合わせ</a>
        """
        session = Mock()
        session.get.return_value = FakeResponse(html)
        found = discover_contact(session, "https://example.jp")
        self.assertEqual(found.form_url, "https://example.jp/contact")
        self.assertEqual(found.form_type, "CONTACT")

    def test_discover_contact_marks_booking_only_ambiguous(self):
        html = "<a href='/booking'>予約はこちら</a>"
        session = Mock()
        session.get.return_value = FakeResponse(html)
        found = discover_contact(session, "https://example.jp")
        self.assertEqual(found.form_type, "AMBIGUOUS")
        self.assertEqual(found.form_url, "https://example.jp/booking")

    def test_insert_candidate_is_idempotent(self):
        td, _, conn = self.make_db()
        try:
            data = {
                "place_id": "P1",
                "company_name": "Test Salon",
                "category": "ネイルサロン",
                "area": "横浜駅",
                "website_url": "https://example.jp",
                "rating": 4.5,
                "user_ratings_total": 10,
                "lifecycle_status": "CANDIDATE",
                "source": "lead-acquisition-v1",
                "created_at": "2026-08-29T00:00:00+00:00",
            }
            first = insert_candidate(conn, data)
            second = insert_candidate(conn, data)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(existing_place_ids(conn), {"P1"})
        finally:
            conn.close()
            td.cleanup()

    def test_update_contact_adds_legacy_columns(self):
        td, _, conn = self.make_db()
        try:
            lead_id = insert_candidate(conn, {
                "place_id": "P2",
                "company_name": "Shop",
                "category": "美容室",
                "area": "川崎駅",
                "created_at": "2026-08-29T00:00:00+00:00",
            })
            update_contact(conn, lead_id, ContactDiscovery(
                email="info@shop.jp",
                form_url="https://shop.jp/contact",
                form_type="CONTACT",
            ))
            row = conn.execute(
                "SELECT legacy_email, legacy_form_url FROM leads WHERE lead_id=?", (lead_id,)
            ).fetchone()
            self.assertEqual(row, ("info@shop.jp", "https://shop.jp/contact"))
        finally:
            conn.close()
            td.cleanup()


if __name__ == "__main__":
    unittest.main()

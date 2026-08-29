import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lead_discovery import (
    PageData,
    choose_contact,
    decode_ddg_href,
    extract_email,
    insert_lead,
    synthetic_place_id,
)


class LeadDiscoveryTest(unittest.TestCase):
    def make_db(self):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "lead.db"
        conn = sqlite3.connect(db)
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
        return td, db, conn

    def test_decode_ddg_redirect(self):
        url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.jp%2Fshop%3Fx%3D1"
        self.assertEqual(decode_ddg_href(url), "https://example.jp/shop")

    def test_extract_email_skips_junk(self):
        self.assertEqual(
            extract_email("sample@example.jp support@real-salon.jp"),
            "support@real-salon.jp",
        )

    def test_choose_contact_ignores_booking(self):
        links = [
            ("/reserve", "予約はこちら"),
            ("/contact", "お問い合わせ"),
        ]
        self.assertEqual(
            choose_contact("https://salon.jp", links),
            "https://salon.jp/contact",
        )

    def test_synthetic_place_id_is_host_stable(self):
        a = synthetic_place_id("https://salon.jp/a")
        b = synthetic_place_id("https://salon.jp/b")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("web:"))

    def test_insert_lead_persists_intelligence(self):
        td, _, conn = self.make_db()
        try:
            page = PageData(
                url="https://salon.jp",
                text="Salon page",
                email="info@salon.jp",
                contact_form_url="https://salon.jp/contact",
                links=(),
            )
            intel = {
                "company_name": "Salon Test",
                "business_type": "ネイルサロン",
                "area": "横浜",
                "is_target": True,
                "confidence": 0.91,
                "email": "info@salon.jp",
                "contact_form_url": "https://salon.jp/contact",
                "services": ["ジェルネイル"],
                "strengths": ["駅近"],
                "lp_opportunities": ["デザイン数を訴求"],
            }
            lead_id = insert_lead(
                conn, page, intel, "横浜", "ネイルサロン", "横浜 ネイルサロン", "qwen2.5:7b"
            )
            conn.commit()
            self.assertIsNotNone(lead_id)
            row = conn.execute(
                "SELECT company_name, legacy_email, legacy_form_url FROM leads WHERE lead_id=?",
                (lead_id,),
            ).fetchone()
            self.assertEqual(row, ("Salon Test", "info@salon.jp", "https://salon.jp/contact"))
            raw = conn.execute(
                "SELECT intelligence_json FROM lead_discovery_intelligence WHERE lead_id=?",
                (lead_id,),
            ).fetchone()[0]
            saved = json.loads(raw)
            self.assertEqual(saved["strengths"], ["駅近"])
        finally:
            conn.close()
            td.cleanup()


if __name__ == "__main__":
    unittest.main()

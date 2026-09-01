from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lp_production


TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<title>生成AI活用型 事前診断・集客・予約最適化システム | 合同会社Nexcess</title>
<meta name="description" content="集客からAI事前診断、予約確定まで一体化。営業・受付業務を自動化する販売業務支援システム。まず無料で適合診断を受けてください。">
</head>
<body>
<section id="hero">
  <div class="hero-eyebrow">生成AI × 販売業務支援システム</div>
  <h1 class="hero-title">
    集客から予約確定まで、<em>AIが自動で動かす。</em>
  </h1>
  <p class="hero-sub">事前診断・顧客スコアリング・予約連動を一体化。営業・受付の工数を削減しながら、成約率を高めます。</p>
</section>

<!-- ─── PAIN -->
</body>
</html>
"""


class LPProductionE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "lead_intelligence.db"
        self.template = self.root / "index.html"
        self.output = self.root / "generated"
        self.template.write_text(TEMPLATE, encoding="utf-8")

        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE leads (
              lead_id INTEGER PRIMARY KEY,
              company_name TEXT,
              category TEXT,
              area TEXT,
              website_url TEXT
            );
            CREATE TABLE screening_decisions (
              lead_id INTEGER PRIMARY KEY,
              decision TEXT NOT NULL
            );
            CREATE TABLE lead_discovery_intelligence (
              lead_id INTEGER PRIMARY KEY,
              intelligence_json TEXT
            );
            CREATE TABLE sales_queue (
              queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
              lead_id INTEGER NOT NULL,
              campaign_id TEXT NOT NULL,
              company_name TEXT,
              website_url TEXT,
              status TEXT NOT NULL DEFAULT 'READY',
              created_at TEXT,
              bridge_version TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO leads VALUES (1, 'テストネイル', 'ネイルサロン', '渋谷', 'https://example.com')"
        )
        conn.execute("INSERT INTO screening_decisions VALUES (1, 'GO')")
        intel = {
            "company_name": "テストネイル",
            "business_type": "ネイルサロン",
            "area": "渋谷",
            "strengths": ["丁寧な施術"],
            "lp_opportunities": ["予約導線を分かりやすくできる"],
        }
        conn.execute(
            "INSERT INTO lead_discovery_intelligence VALUES (?, ?)",
            (1, json.dumps(intel, ensure_ascii=False)),
        )
        conn.execute(
            """
            INSERT INTO sales_queue
              (lead_id, campaign_id, company_name, website_url, status)
            VALUES (1, 'TEST-CAMPAIGN', 'テストネイル', 'https://example.com', 'READY')
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @patch("lp_production.generate_copy")
    def test_go_lead_generates_html_and_marks_generated(self, mock_copy):
        mock_copy.return_value = {
            "eyebrow": "テストネイル様向け Path-Flow診断",
            "headline": "テストネイルの魅力を、予約につながる導線へ。",
            "subheadline": "渋谷のネイルサロン向け個別提案です。",
            "diagnosis": "予約導線を分かりやすくできる余地があります。",
            "strength": "丁寧な施術が強みです。",
            "diagnostic_questions": [
                {
                    "id": "goal",
                    "text": "今回、一番変えたいのはどこですか？",
                    "options": ["顔まわり", "長さ", "カラー"]
                }
            ],
            "customer_voice_version": "customer-voice-hair-v1",
        }

        with patch.object(lp_production, "REPO_ROOT", self.root):
            result = lp_production.generate(
                self.db,
                self.template,
                self.output,
                1,
                "http://127.0.0.1:11434",
                "qwen2.5:7b",
            )

        self.assertEqual(result["lead_id"], 1)
        self.assertEqual(result["lp_status"], "GENERATED")
        self.assertEqual(result["qa_status"], "PASS")
        self.assertEqual(result["quality_status"], "QA_PASS")
        html = (self.output / "1" / "index.html").read_text(encoding="utf-8")
        self.assertIn("テストネイル", html)
        self.assertIn("予約につながる導線", html)
        self.assertIn('<meta name="pathflow-lead-id" content="1">', html)
        self.assertIn("CUSTOMER VOICE", html)
        self.assertIn("今回、一番変えたいのはどこですか？", html)
        self.assertNotIn("4,500,000", html)
        self.assertNotIn("企業規模", html)
        self.assertNotIn("ROI ESTIMATE", html)
        self.assertNotIn("生成AI事前診断エンジン", html)
        self.assertNotIn("/* ─── SOLUTION", html)
        self.assertNotIn("/* ─── PRICING", html)
        self.assertNotIn("/* ─── DIAGNOSIS OVERLAY", html)
        self.assertNotIn("#diag-overlay", html)

        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT lp_status, deploy_status, lp_url FROM sales_queue WHERE lead_id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("GENERATED", "PENDING", None))

    @patch("lp_production.verify_deployed")
    def test_lp_url_is_saved_only_after_deploy_verification(self, verify):
        lp_production.ensure_queue_schema(sqlite3.connect(self.db))
        result = lp_production.mark_deployed(
            self.db, 1, "https://preview.example.com/generated/1/"
        )
        verify.assert_called_once_with(
            "https://preview.example.com/generated/1/", 1, "テストネイル"
        )
        self.assertEqual(result["deploy_status"], "READY")

        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT lp_status, deploy_status, lp_url FROM sales_queue WHERE lead_id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(
            row,
            ("DEPLOYED", "READY", "https://preview.example.com/generated/1/"),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
from pathlib import Path

from ollama_client import OllamaClient
from response_watcher import apply_classification

TEST_DB = Path("sales_engine_agent_test.db")
CAMPAIGN_ID = "PF-NAIL-001"

CASES = [
    ("A001", "詳しく聞きたいので、一度打ち合わせできますか？", "INTERESTED", "HUMAN_ACTION", 1),
    ("A002", "料金はいくらですか？", "PRICE_INQUIRY", "HUMAN_ACTION", 1),
    ("A003", "今回は不要です。今後の案内も不要です。", "DECLINED", "LOST", 0),
    ("A004", "自動返信です。お問い合わせを受け付けました。", "AUTO_REPLY", "CLOSED_NO_RESPONSE", 0),
]


def init_db() -> sqlite3.Connection:
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
    for store_id, *_ in CASES:
        conn.execute(
            "INSERT INTO leads (campaign_id, store_id, store_name, email) VALUES (?, ?, ?, ?)",
            (CAMPAIGN_ID, store_id, f"テスト店舗{store_id}", f"{store_id.lower()}@example.com"),
        )
    conn.commit()
    return conn


def main() -> None:
    ai = OllamaClient()
    conn = init_db()
    passed = 0
    try:
        for i, (store_id, body, expected_label, expected_status, expected_human) in enumerate(CASES, start=1):
            result = ai.classify_response(
                store_name=f"テスト店舗{store_id}",
                subject="Re: 来店前受付ページについて",
                body_preview=body,
            )
            apply_classification(conn, store_id, result, f"agent-test-{i}")
            conn.commit()
            row = conn.execute(
                "SELECT sales_status, response_type, human_action FROM leads WHERE campaign_id=? AND store_id=?",
                (CAMPAIGN_ID, store_id),
            ).fetchone()
            ok = (
                result["label"] == expected_label
                and row[0] == expected_status
                and row[1] == expected_label
                and row[2] == expected_human
            )
            passed += int(ok)
            print(
                f"store={store_id} label={result['label']} status={row[0]} human={row[2]} "
                f"{'PASS' if ok else 'FAIL'}"
            )
        print(f"score={passed}/{len(CASES)}")
        if passed != len(CASES):
            raise SystemExit(1)
    finally:
        conn.close()
        if TEST_DB.exists():
            TEST_DB.unlink()


if __name__ == "__main__":
    main()

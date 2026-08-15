from __future__ import annotations

from ollama_client import OllamaClient

TEST_CASES = [
    ("INTERESTED", "詳しく話を聞きたいです。一度打ち合わせできますか？"),
    ("PRICE_INQUIRY", "料金はいくらですか？月額費用も教えてください。"),
    ("QUESTION", "予約システムとの連携はできますか？"),
    ("CONSIDERING", "ありがとうございます。スタッフと相談して検討します。"),
    ("DECLINED", "今回は不要です。今後の営業連絡も不要です。"),
    ("AUTO_REPLY", "ただいま営業時間外です。このメッセージは自動返信です。"),
    ("UNKNOWN", "確認しました。"),
]


def main() -> None:
    client = OllamaClient()
    ok = 0
    for expected, body in TEST_CASES:
        result = client.classify_response(
            store_name="Path-Flowテスト店舗",
            subject="Re: 来店前受付ページのご案内",
            body_preview=body,
        )
        actual = result.get("label")
        matched = actual == expected
        ok += int(matched)
        print(
            f"expected={expected}\tactual={actual}\t"
            f"confidence={result.get('confidence')}\thuman={result.get('needs_human')}\t"
            f"{'PASS' if matched else 'FAIL'}"
        )
        print(f"reason={result.get('reason')}\n")
    print(f"score={ok}/{len(TEST_CASES)}")


if __name__ == "__main__":
    main()

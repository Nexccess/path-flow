from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "hf.co/Qwen/Qwen2.5-7B-Instruct-GGUF:latest"

RESPONSE_LABELS = (
    "INTERESTED",
    "PRICE_INQUIRY",
    "QUESTION",
    "CONSIDERING",
    "DECLINED",
    "AUTO_REPLY",
    "UNKNOWN",
)

HUMAN_LABELS = {
    "INTERESTED",
    "PRICE_INQUIRY",
    "QUESTION",
    "CONSIDERING",
    "UNKNOWN",
}

PRICE_HINTS = (
    "料金", "価格", "費用", "月額", "初期費用", "契約", "支払", "支払い", "いくら", "円", "プラン",
)
QUESTION_HINTS = (
    "?", "？", "できますか", "可能ですか", "対応していますか", "連携", "使えますか", "教えて", "でしょうか",
)
AUTO_REPLY_HINTS = (
    "自動返信", "自動応答", "不在", "受付完了", "受け付けました", "受信しました", "お問い合わせありがとうございます",
    "autoreply", "auto reply", "out of office", "automatic reply",
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(RESPONSE_LABELS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_human": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["label", "confidence", "needs_human", "reason"],
    "additionalProperties": False,
}

REASON_JA = {
    "INTERESTED": "導入・説明・打合せなど前向きな意思を検出しました。",
    "PRICE_INQUIRY": "料金・費用・契約条件に関する質問を検出しました。",
    "QUESTION": "機能・仕様・連携・使い方などの質問を検出しました。",
    "CONSIDERING": "検討・社内確認など、保留を含む前向きな反応を検出しました。",
    "DECLINED": "不要・連絡停止など、明確な拒否を検出しました。",
    "AUTO_REPLY": "自動返信・不在通知・受付完了通知の特徴を検出しました。",
    "UNKNOWN": "安全に分類できるだけの明確な情報が不足しています。",
}


@dataclass
class OllamaConfig:
    base_url: str
    model: str

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            base_url=os.getenv("PF_OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/"),
            model=os.getenv("PF_OLLAMA_MODEL", DEFAULT_MODEL).strip(),
        )


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(h.lower() in lower for h in hints)


def _guard_label(label: str, text: str) -> str:
    """Deterministic safety guard around LLM classification."""
    if label == "PRICE_INQUIRY" and not _contains_any(text, PRICE_HINTS):
        return "QUESTION" if _contains_any(text, QUESTION_HINTS) else "UNKNOWN"
    if label == "AUTO_REPLY" and not _contains_any(text, AUTO_REPLY_HINTS):
        return "UNKNOWN"
    return label


class OllamaClient:
    def __init__(self, config: OllamaConfig | None = None):
        self.config = config or OllamaConfig.from_env()

    def health(self) -> dict:
        res = requests.get(f"{self.config.base_url}/api/tags", timeout=5)
        res.raise_for_status()
        data = res.json()
        names = [m.get("name") for m in data.get("models", []) if m.get("name")]
        return {"ok": True, "model": self.config.model, "installed_models": names}

    def classify_response(self, *, store_name: str, subject: str, body_preview: str) -> dict:
        prompt = f"""あなたはPath-Flowの営業返信分類エージェントです。
以下の返信内容だけを根拠に、必ず1つだけ分類してください。

重要: 出力する reason は必ず自然な日本語だけで書いてください。中国語、英語、その他の言語を混ぜないでください。

【分類ルール】
INTERESTED: 導入したい、詳しく聞きたい、打合せしたい、説明してほしい等の前向きな意思
PRICE_INQUIRY: 料金、価格、費用、月額、初期費用、契約期間、支払条件を明示的に尋ねている
QUESTION: 機能、仕様、連携、使い方、対応可否などの質問。料金の質問ではない
CONSIDERING: 検討する、社内確認する、後で見る、相談してみる等で、まだ具体的な質問や打合せ希望はない
DECLINED: 不要、興味なし、営業停止、今後連絡不要など明確な拒否
AUTO_REPLY: 自動返信、不在通知、受付完了通知、自動応答であることが本文から明確
UNKNOWN: 内容が短すぎる、曖昧、または上記に安全に分類できない

【厳守する優先ルール】
1. 「料金・価格・費用・月額・初期費用・契約・支払・いくら」等の料金語が本文に無ければ PRICE_INQUIRY にしない。
2. 「連携できますか」「対応していますか」「どう使いますか」等は QUESTION。
3. AUTO_REPLY は、自動返信・不在・受付完了などの明示的特徴がある場合だけ。短いだけの文章を AUTO_REPLY にしない。
4. 「了解です」「わかりました」等、意図が不明な短文は UNKNOWN。
5. 前向きでも、単に「検討します」だけなら CONSIDERING。
6. 判断に迷う場合は UNKNOWN。

needs_human は参考出力であり、最終判定はシステム側で上書きされます。

店舗名: {store_name}
件名: {subject}
本文プレビュー:
{body_preview}
"""
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "options": {"temperature": 0},
        }
        res = requests.post(f"{self.config.base_url}/api/generate", json=payload, timeout=60)
        res.raise_for_status()
        raw = res.json().get("response", "")
        data = json.loads(raw)
        label = data.get("label")
        if label not in RESPONSE_LABELS:
            raise ValueError(f"Unexpected response label: {label}")

        full_text = f"{subject}\n{body_preview}"
        guarded_label = _guard_label(label, full_text)
        confidence = float(data.get("confidence", 0))
        if guarded_label != label:
            confidence = min(confidence, 0.80)

        data["label"] = guarded_label
        data["confidence"] = confidence
        # Never expose mixed-language model reasoning to operators; use deterministic Japanese summaries.
        data["reason"] = REASON_JA[guarded_label]
        # Safety policy is deterministic. Never let the LLM decide whether a human must see a reply.
        data["needs_human"] = guarded_label in HUMAN_LABELS
        return data

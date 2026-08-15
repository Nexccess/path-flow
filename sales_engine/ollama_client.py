from __future__ import annotations

import json
import os
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

【分類ルール】
INTERESTED: 導入したい、詳しく聞きたい、打合せしたい、説明してほしい等の前向きな意思
PRICE_INQUIRY: 料金、価格、費用、月額、初期費用、契約期間、支払条件を尋ねている
QUESTION: 機能、仕様、連携、使い方、対応可否などの質問。料金の質問ではない
CONSIDERING: 検討する、社内確認する、後で見る、相談してみる等で、まだ具体的な質問や打合せ希望はない
DECLINED: 不要、興味なし、営業停止、今後連絡不要など明確な拒否
AUTO_REPLY: 不在通知、受付完了通知、自動応答
UNKNOWN: 内容が短すぎる、曖昧、または上記に安全に分類できない

【優先ルール】
1. 「料金・価格・費用・月額・初期費用・契約・支払」に直接触れていなければ PRICE_INQUIRY にしない。
2. 「連携できますか」「対応していますか」「どう使いますか」等は QUESTION。
3. 前向きでも、単に「検討します」だけなら CONSIDERING。
4. 判断に迷う場合は UNKNOWN。

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

        data["confidence"] = float(data.get("confidence", 0))
        # Safety policy is deterministic. Never let the LLM decide whether a human must see a reply.
        data["needs_human"] = label in HUMAN_LABELS
        return data

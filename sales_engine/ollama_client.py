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
以下の返信内容だけを根拠に分類してください。

【分類】
INTERESTED: 導入・打合せ・詳しい説明を前向きに希望
PRICE_INQUIRY: 料金・費用・契約条件について質問
QUESTION: 機能・仕様・使い方など一般的な質問
CONSIDERING: 検討する、社内確認する、後で見る等
DECLINED: 不要、営業停止、今後連絡不要など明確な拒否
AUTO_REPLY: 不在通知、受付完了、自動返信
UNKNOWN: 上記に安全に分類できない

【人間対応】
INTERESTED / PRICE_INQUIRY / QUESTION / CONSIDERING / UNKNOWN は needs_human=true。
DECLINED / AUTO_REPLY は needs_human=false。
判断に迷う場合は UNKNOWN としてください。

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
        if data.get("label") not in RESPONSE_LABELS:
            raise ValueError(f"Unexpected response label: {data.get('label')}")
        data["confidence"] = float(data.get("confidence", 0))
        return data

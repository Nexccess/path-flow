from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
JST = timezone(timedelta(hours=9))
PF_TAG_RE = re.compile(r"\[PF:([^\]]+)\]")


@dataclass
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str

    @classmethod
    def from_env(cls) -> "GraphConfig":
        values = {
            "tenant_id": os.getenv("PF_MS_TENANT_ID", "").strip(),
            "client_id": os.getenv("PF_MS_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("PF_MS_CLIENT_SECRET", "").strip(),
            "mailbox": os.getenv("PF_MS_MAILBOX", "info@nexccess.com").strip(),
        }
        missing = [k for k in ("tenant_id", "client_id", "client_secret") if not values[k]]
        if missing:
            raise RuntimeError(f"Missing Graph settings: {', '.join(missing)}")
        return cls(**values)


class GraphMailClient:
    def __init__(self, config: GraphConfig):
        self.config = config
        self._token: str | None = None
        self._expires_at = 0.0

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        token_url = f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token"
        res = requests.post(
            token_url,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        res.raise_for_status()
        data = res.json()
        self._token = data["access_token"]
        self._expires_at = now + int(data.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

    def send_text(self, to_address: str, subject: str, body: str, *, dry_run: bool = True) -> dict:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_address}}],
            },
            "saveToSentItems": True,
        }
        if dry_run:
            return {"dry_run": True, "to": to_address, "subject": subject, "payload": payload}

        mailbox = quote(self.config.mailbox, safe="@")
        res = requests.post(
            f"{GRAPH_ROOT}/users/{mailbox}/sendMail",
            headers=self._headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=20,
        )
        if res.status_code != 202:
            raise RuntimeError(f"Graph sendMail failed: {res.status_code} {res.text[:1000]}")
        return {"dry_run": False, "accepted": True, "status_code": res.status_code}

    def recent_inbox(self, since: datetime, *, top: int = 100) -> list[dict]:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        since_utc = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        mailbox = quote(self.config.mailbox, safe="@")
        params = {
            "$select": "id,subject,receivedDateTime,from,replyTo,internetMessageId,conversationId,bodyPreview,isRead",
            "$filter": f"receivedDateTime ge {since_utc}",
            "$orderby": "receivedDateTime desc",
            "$top": str(top),
        }
        res = requests.get(
            f"{GRAPH_ROOT}/users/{mailbox}/mailFolders/inbox/messages",
            headers=self._headers(),
            params=params,
            timeout=20,
        )
        res.raise_for_status()
        return res.json().get("value", [])


def extract_store_id(subject: str | None) -> str | None:
    if not subject:
        return None
    m = PF_TAG_RE.search(subject)
    return m.group(1).strip() if m else None


def sender_address(message: dict) -> str | None:
    try:
        return message["from"]["emailAddress"]["address"].strip().lower()
    except Exception:
        return None

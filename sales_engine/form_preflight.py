from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from engine import add_event, now_iso

CAMPAIGN_ID = "PF-NAIL-001"
USER_AGENT = "Mozilla/5.0 (compatible; PathFlowSalesAgent/1.0; +https://sample.pathflow.org)"
TIMEOUT_SECONDS = 10

CAPTCHA_HINTS = (
    "recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile", "turnstile", "captcha"
)
BLOCK_PHRASES = (
    "営業目的", "営業メール", "営業のご連絡", "営業の問い合わせ", "セールス目的",
    "勧誘目的", "売り込み", "営業・勧誘", "営業等", "営業はお断り", "営業お断り",
    "sales solicitation", "no solicitation", "solicitation prohibited",
)
CONTACT_HINTS = (
    "お問い合わせ", "問い合わせ", "contact", "inquiry", "ご相談", "相談"
)
FIELD_HINTS = {
    "name": ("name", "お名前", "氏名", "担当者名", "company", "会社名", "店舗名"),
    "email": ("email", "mail", "メール"),
    "message": ("message", "body", "content", "detail", "お問い合わせ内容", "問合せ内容", "内容", "ご相談内容"),
}


@dataclass
class FormInfo:
    action: str
    method: str
    fields: list[dict]


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[FormInfo] = []
        self._current: FormInfo | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_d = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._current = FormInfo(
                action=attrs_d.get("action", ""),
                method=(attrs_d.get("method") or "get").lower(),
                fields=[],
            )
        elif self._current is not None and tag in {"input", "textarea", "select"}:
            self._current.fields.append({
                "tag": tag,
                "name": attrs_d.get("name", ""),
                "type": attrs_d.get("type", ""),
                "id": attrs_d.get("id", ""),
                "placeholder": attrs_d.get("placeholder", ""),
                "required": "required" in attrs_d,
                "value": attrs_d.get("value", ""),
            })

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def fetch_html(url: str) -> tuple[str | None, str | None, int | None]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            content_type = (res.headers.get("Content-Type") or "").lower()
            if "html" not in content_type:
                return None, res.geturl(), getattr(res, "status", None)
            raw = res.read(2_000_000)
            charset = res.headers.get_content_charset() or "utf-8"
            try:
                html = raw.decode(charset, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return html, res.geturl(), getattr(res, "status", None)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None, None, None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def field_matches(field: dict, hints: tuple[str, ...]) -> bool:
    hay = normalize_text(" ".join(str(field.get(k, "")) for k in ("name", "id", "placeholder", "type")))
    return any(h.lower() in hay for h in hints)


def inspect_form(url: str) -> dict:
    html, final_url, status_code = fetch_html(url)
    if not html or not final_url:
        return {"decision": "FETCH_FAILED", "send_allowed": False, "reason": "フォームページを取得できませんでした。", "url": url}

    low = html.lower()
    if any(h in low for h in CAPTCHA_HINTS):
        return {"decision": "BLOCKED_CAPTCHA", "send_allowed": False, "reason": "CAPTCHA/Turnstile等を検出しました。自動突破しません。", "url": final_url}

    block_phrase = next((p for p in BLOCK_PHRASES if p.lower() in low), None)
    if block_phrase:
        return {"decision": "BLOCKED_POLICY", "send_allowed": False, "reason": f"営業利用を制限する文言を検出: {block_phrase}", "url": final_url}

    parser = FormParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    candidates: list[dict] = []
    for idx, form in enumerate(parser.forms):
        method = form.method.lower()
        action = urljoin(final_url, form.action or final_url)
        has_email = any(field_matches(f, FIELD_HINTS["email"]) for f in form.fields)
        has_message = any(field_matches(f, FIELD_HINTS["message"]) or f.get("tag") == "textarea" for f in form.fields)
        hidden_count = sum(1 for f in form.fields if str(f.get("type", "")).lower() == "hidden")
        candidates.append({
            "index": idx,
            "method": method,
            "action": action,
            "field_count": len(form.fields),
            "hidden_count": hidden_count,
            "has_email": has_email,
            "has_message": has_message,
            "fields": form.fields,
        })

    usable = [f for f in candidates if f["method"] == "post" and f["has_email"] and f["has_message"]]
    if not usable:
        return {
            "decision": "REVIEW_REQUIRED",
            "send_allowed": False,
            "reason": "POST形式でメール欄と本文欄を特定できるフォームがありません。",
            "url": final_url,
            "forms": candidates,
        }

    selected = max(usable, key=lambda f: (f["has_message"], f["has_email"], f["field_count"]))
    action_host = urlparse(selected["action"]).netloc.lower()
    page_host = urlparse(final_url).netloc.lower()
    same_host = action_host == page_host or action_host.endswith("." + page_host) or page_host.endswith("." + action_host)
    if not same_host:
        return {
            "decision": "REVIEW_REQUIRED",
            "send_allowed": False,
            "reason": "フォーム送信先が別ドメインです。自動送信前に確認が必要です。",
            "url": final_url,
            "selected_form": selected,
        }

    return {
        "decision": "AUTO_READY",
        "send_allowed": True,
        "reason": "CAPTCHA・営業禁止文言を検出せず、同一サイトのPOSTフォームを特定しました。",
        "url": final_url,
        "selected_form": selected,
        "http_status": status_code,
    }


def run(db: Path, limit: int | None = None, apply: bool = False) -> dict:
    conn = sqlite3.connect(db)
    counts: dict[str, int] = {}
    try:
        sql = """
            SELECT store_id, store_name, contact_form_url
            FROM leads
            WHERE campaign_id=? AND contact_status='READY_FORM' AND contact_form_url IS NOT NULL
            ORDER BY store_id
        """
        params: list[object] = [CAMPAIGN_ID]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        for store_id, store_name, form_url in rows:
            result = inspect_form(form_url)
            decision = result["decision"]
            counts[decision] = counts.get(decision, 0) + 1
            print(f"{decision}\t{store_id}\t{store_name}\t{form_url}")
            print("reason=" + result.get("reason", ""))

            if apply:
                allowed = 1 if result.get("send_allowed") else 0
                screening = {
                    "AUTO_READY": "FORM_AUTO_READY",
                    "BLOCKED_CAPTCHA": "FORM_BLOCKED_CAPTCHA",
                    "BLOCKED_POLICY": "FORM_BLOCKED_POLICY",
                    "FETCH_FAILED": "FORM_FETCH_FAILED",
                }.get(decision, "FORM_REVIEW")
                conn.execute(
                    """
                    UPDATE leads
                    SET send_allowed=?, screening_status=?, contact_checked_at=?, updated_at=?
                    WHERE campaign_id=? AND store_id=?
                    """,
                    (allowed, screening, now_iso(), now_iso(), CAMPAIGN_ID, store_id),
                )
                add_event(conn, store_id, "FORM_PREFLIGHT", result)
                conn.commit()

        summary = {"targets": len(rows), **counts, "applied": apply}
        print("summary=" + json.dumps(summary, ensure_ascii=False))
        return summary
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Audit inquiry forms before any automated submission.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--limit", type=int)
    p.add_argument("--apply", action="store_true", help="Persist preflight decisions to the sales ledger. Does not submit any form.")
    args = p.parse_args()
    run(args.db, limit=args.limit, apply=args.apply)


if __name__ == "__main__":
    main()

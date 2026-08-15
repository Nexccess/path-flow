from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))
USER_AGENT = "Mozilla/5.0 (compatible; PathFlowContactEnrichment/0.1; +https://sample.pathflow.org)"
TIMEOUT_SECONDS = 8
MAX_LINKS_TO_FOLLOW = 8

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CONTACT_HINTS = (
    "contact", "inquiry", "お問い合わせ", "問い合わせ", "ご相談", "予約", "reserve",
    "company", "about", "店舗情報", "サロン情報"
)
LINE_HINTS = ("line.me", "lin.ee")
INSTAGRAM_HINTS = ("instagram.com",)
EXCLUDED_HOST_HINTS = (
    "beauty.hotpepper.jp", "maps.google.", "google.com/maps", "tabelog.com",
    "facebook.com", "x.com", "twitter.com"
)


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs_d = dict(attrs)
            self._current_href = attrs_d.get("href")
            self._text = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current_href is not None:
            self.links.append((self._current_href, " ".join(self._text).strip()))
            self._current_href = None
            self._text = []


def fetch_html(url: str) -> tuple[str | None, str | None]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            content_type = (res.headers.get("Content-Type") or "").lower()
            if "html" not in content_type:
                return None, res.geturl()
            raw = res.read(1_500_000)
            charset = res.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace"), res.geturl()
            except LookupError:
                return raw.decode("utf-8", errors="replace"), res.geturl()
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None, None


def same_site(base: str, target: str) -> bool:
    try:
        a = urlparse(base).netloc.lower().split(":")[0]
        b = urlparse(target).netloc.lower().split(":")[0]
        return a == b or a.endswith("." + b) or b.endswith("." + a)
    except Exception:
        return False


def is_excluded_source(url: str | None) -> bool:
    if not url:
        return True
    low = url.lower()
    return any(h in low for h in EXCLUDED_HOST_HINTS)


def extract_from_page(page_url: str, html: str):
    parser = LinkParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    emails = set(EMAIL_RE.findall(html))
    forms: list[str] = []
    lines: list[str] = []
    instagrams: list[str] = []
    candidate_pages: list[str] = []

    for href, text in parser.links:
        href = (href or "").strip()
        if not href:
            continue
        full = urljoin(page_url, href)
        low = full.lower()
        text_low = text.lower()
        if low.startswith("mailto:"):
            addr = href[7:].split("?", 1)[0].strip()
            if addr:
                emails.add(addr)
            continue
        if any(h in low for h in LINE_HINTS):
            lines.append(full)
            continue
        if any(h in low for h in INSTAGRAM_HINTS):
            instagrams.append(full)
            continue
        if full.startswith("http") and same_site(page_url, full):
            hint_text = low + " " + text_low
            if any(h in hint_text for h in CONTACT_HINTS):
                candidate_pages.append(full)
                if any(k in hint_text for k in ("contact", "inquiry", "お問い合わせ", "問い合わせ")):
                    forms.append(full)

    return {
        "emails": sorted(emails),
        "forms": list(dict.fromkeys(forms)),
        "line": list(dict.fromkeys(lines)),
        "instagram": list(dict.fromkeys(instagrams)),
        "candidate_pages": list(dict.fromkeys(candidate_pages)),
    }


def enrich_url(start_url: str):
    html, final_url = fetch_html(start_url)
    if not html or not final_url:
        return {"status": "FETCH_FAILED", "source": start_url}

    result = extract_from_page(final_url, html)
    visited = {final_url}
    for url in result["candidate_pages"][:MAX_LINKS_TO_FOLLOW]:
        if url in visited:
            continue
        visited.add(url)
        sub_html, sub_final = fetch_html(url)
        if not sub_html or not sub_final:
            continue
        sub = extract_from_page(sub_final, sub_html)
        for key in ("emails", "forms", "line", "instagram"):
            result[key] = list(dict.fromkeys(result[key] + sub[key]))
        time.sleep(0.15)

    email = result["emails"][0] if result["emails"] else None
    form = result["forms"][0] if result["forms"] else None
    line = result["line"][0] if result["line"] else None
    instagram = result["instagram"][0] if result["instagram"] else None

    if email:
        status, channel, confidence, allowed = "READY_EMAIL", "email", "HIGH", 1
    elif form:
        status, channel, confidence, allowed = "READY_FORM", "form", "MEDIUM", 0
    elif line:
        status, channel, confidence, allowed = "READY_LINE", "line", "MEDIUM", 0
    elif instagram:
        status, channel, confidence, allowed = "READY_INSTAGRAM", "instagram", "MEDIUM", 0
    else:
        status, channel, confidence, allowed = "NO_CONTACT", None, "LOW", 0

    return {
        "status": status,
        "channel": channel,
        "email": email,
        "form": form,
        "line": line,
        "instagram": instagram,
        "source": final_url,
        "confidence": confidence,
        "send_allowed": allowed,
    }


def migrate_contact_columns(conn: sqlite3.Connection):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    additions = {
        "contact_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "primary_channel": "TEXT",
        "email": "TEXT",
        "contact_form_url": "TEXT",
        "line_url": "TEXT",
        "instagram_url": "TEXT",
        "contact_source_url": "TEXT",
        "contact_checked_at": "TEXT",
        "contact_confidence": "TEXT",
        "send_allowed": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, spec in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {spec}")
    conn.commit()


def run(db: Path, limit: int | None = None, force: bool = False):
    conn = sqlite3.connect(db)
    try:
        migrate_contact_columns(conn)
        sql = """
            SELECT store_id, store_name, store_url, contact_status
            FROM leads
            WHERE campaign_id=?
        """
        params: list[object] = [CAMPAIGN_ID]
        if not force:
            sql += " AND (contact_status IS NULL OR contact_status='PENDING')"
        sql += " ORDER BY store_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        for store_id, store_name, store_url, _ in rows:
            if not store_url or is_excluded_source(store_url):
                result = {
                    "status": "MANUAL_CHECK" if store_url else "NO_CONTACT",
                    "channel": None,
                    "email": None,
                    "form": None,
                    "line": None,
                    "instagram": None,
                    "source": store_url,
                    "confidence": "LOW",
                    "send_allowed": 0,
                }
            else:
                result = enrich_url(store_url)
                if result.get("status") == "FETCH_FAILED":
                    result.update({
                        "channel": None, "email": None, "form": None, "line": None,
                        "instagram": None, "confidence": "LOW", "send_allowed": 0,
                    })

            conn.execute(
                """
                UPDATE leads SET
                  contact_status=?, primary_channel=?, email=?, contact_form_url=?,
                  line_url=?, instagram_url=?, contact_source_url=?, contact_checked_at=?,
                  contact_confidence=?, send_allowed=?, screening_status=?, updated_at=?
                WHERE campaign_id=? AND store_id=?
                """,
                (
                    result["status"], result.get("channel"), result.get("email"), result.get("form"),
                    result.get("line"), result.get("instagram"), result.get("source"), now_iso(),
                    result.get("confidence"), result.get("send_allowed", 0),
                    "READY" if result.get("send_allowed") else "REVIEW",
                    now_iso(), CAMPAIGN_ID, store_id,
                ),
            )
            conn.commit()
            print(f"{store_id}\t{store_name}\t{result['status']}\t{result.get('channel') or '-'}")
            time.sleep(0.2)
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    run(args.db, args.limit, args.force)


if __name__ == "__main__":
    main()

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
USER_AGENT = "Mozilla/5.0 (compatible; PathFlowContactEnrichment/0.4; +https://sample.pathflow.org)"
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
UNRESOLVED_STATUSES = (
    "PENDING", "LEGACY_NO_CONTACT", "NO_CONTACT", "MANUAL_CHECK", "FETCH_FAILED", "DISCOVERY_REQUIRED"
)
CHANNEL_ORDER = (
    "READY_EMAIL",
    "READY_FORM",
    "READY_LINE",
    "READY_INSTAGRAM",
    "READY_SMS",
    "DISCOVERY_REQUIRED",
    "MANUAL_CHECK",
    "NO_CONTACT",
)


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits or None


def sms_capable(phone: str | None) -> bool:
    normalized = normalize_phone(phone)
    return bool(normalized and re.fullmatch(r"0(?:70|80|90)\d{8}", normalized))


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


def choose_start_url(contact_source_url: str | None, store_url: str | None) -> str | None:
    for candidate in (contact_source_url, store_url):
        if candidate and not is_excluded_source(candidate):
            return candidate
    return contact_source_url or store_url


def choose_channel(
    email: str | None,
    form: str | None,
    line: str | None,
    instagram: str | None,
    phone: str | None,
    fallback_status: str,
    fallback_confidence: str,
):
    if email:
        return "READY_EMAIL", "email", "HIGH", 1
    if form:
        return "READY_FORM", "form", "MEDIUM", 0
    if line:
        return "READY_LINE", "line", "MEDIUM", 0
    if instagram:
        return "READY_INSTAGRAM", "instagram", "MEDIUM", 0
    if sms_capable(phone):
        return "READY_SMS", "sms", "MEDIUM", 0
    status = fallback_status if fallback_status in {"MANUAL_CHECK", "NO_CONTACT", "FETCH_FAILED"} else "NO_CONTACT"
    return status, None, fallback_confidence or "LOW", 0


def print_summary(conn: sqlite3.Connection) -> None:
    counts = {
        status: conn.execute(
            "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND contact_status=?",
            (CAMPAIGN_ID, status),
        ).fetchone()[0]
        for status in CHANNEL_ORDER
    }
    known = sum(counts.values())
    total = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=?", (CAMPAIGN_ID,)
    ).fetchone()[0]
    other = total - known
    print("--- channel_summary ---")
    for status in CHANNEL_ORDER:
        print(f"{status}={counts[status]}")
    if other:
        print(f"OTHER={other}")
    print(f"TOTAL={total}")


def run(
    db: Path,
    limit: int | None = None,
    force: bool = False,
    summary_only: bool = False,
    status_filter: str | None = None,
):
    conn = sqlite3.connect(db)
    try:
        migrate_contact_columns(conn)
        if summary_only:
            print_summary(conn)
            return

        sql = """
            SELECT store_id, store_name, store_url, contact_source_url, contact_status,
                   email, contact_form_url, line_url, instagram_url, phone
            FROM leads
            WHERE campaign_id=?
        """
        params: list[object] = [CAMPAIGN_ID]
        if status_filter:
            sql += " AND contact_status=?"
            params.append(status_filter)
        elif not force:
            placeholders = ",".join("?" for _ in UNRESOLVED_STATUSES)
            sql += f" AND (contact_status IS NULL OR contact_status IN ({placeholders}))"
            params.extend(UNRESOLVED_STATUSES)
        sql += " ORDER BY store_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        print(f"enrichment_targets={len(rows)} force={force} status_filter={status_filter or '-'}")
        for (
            store_id, store_name, store_url, contact_source_url, _,
            existing_email, existing_form, existing_line, existing_instagram, phone,
        ) in rows:
            start_url = choose_start_url(contact_source_url, store_url)
            if not start_url or is_excluded_source(start_url):
                result = {
                    "status": "MANUAL_CHECK" if start_url else "NO_CONTACT",
                    "channel": None,
                    "email": None,
                    "form": None,
                    "line": None,
                    "instagram": None,
                    "source": start_url,
                    "confidence": "LOW",
                    "send_allowed": 0,
                }
            else:
                result = enrich_url(start_url)
                if result.get("status") == "FETCH_FAILED":
                    result.update({
                        "channel": None, "email": None, "form": None, "line": None,
                        "instagram": None, "confidence": "LOW", "send_allowed": 0,
                    })

            email = result.get("email") or existing_email
            form = result.get("form") or existing_form
            line = result.get("line") or existing_line
            instagram = result.get("instagram") or existing_instagram

            status, channel, confidence, allowed = choose_channel(
                email,
                form,
                line,
                instagram,
                phone,
                result.get("status", "NO_CONTACT"),
                result.get("confidence", "LOW"),
            )

            conn.execute(
                """
                UPDATE leads SET
                  contact_status=?, primary_channel=?, email=?, contact_form_url=?,
                  line_url=?, instagram_url=?, contact_source_url=COALESCE(?, contact_source_url),
                  contact_checked_at=?, contact_confidence=?, send_allowed=?, screening_status=?, updated_at=?
                WHERE campaign_id=? AND store_id=?
                """,
                (
                    status, channel, email, form, line, instagram, result.get("source"), now_iso(),
                    confidence, allowed,
                    "READY" if status == "READY_EMAIL" else ("FORM_READY" if status == "READY_FORM" else "REVIEW"),
                    now_iso(), CAMPAIGN_ID, store_id,
                ),
            )
            conn.commit()
            print(f"{store_id}\t{store_name}\t{status}\t{channel or '-'}\t{start_url or '-'}")
            time.sleep(0.2)

        print_summary(conn)
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--summary", action="store_true", help="Print channel counts without enrichment")
    p.add_argument("--status", help="Only enrich leads with this exact contact_status")
    args = p.parse_args()
    run(args.db, args.limit, args.force, args.summary, args.status)


if __name__ == "__main__":
    main()

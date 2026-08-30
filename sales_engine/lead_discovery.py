from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from screening_automation import is_portal, looks_booking_only, valid_email

VERSION = "lead-discovery-v1"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
BLOCKED_HOST_HINTS = (
    "duckduckgo.com", "google.com", "google.co.jp", "yahoo.co.jp", "bing.com",
    "instagram.com", "facebook.com", "x.com", "twitter.com", "youtube.com",
    "hotpepper.jp", "tabelog.com", "mapion.co.jp", "ekiten.jp",
)
CONTACT_HINTS = (
    "contact", "inquiry", "otoiawase", "お問い合わせ", "問い合わせ", "お問合せ", "ご相談",
)
NEGATIVE_CONTACT_HINTS = (
    "reserve", "reservation", "booking", "予約", "採用", "recruit", "求人", "support", "faq",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def host_of(url: str | None) -> str:
    try:
        return urlparse(clean(url) or "").netloc.lower().split(":")[0]
    except Exception:
        return ""


def normalize_url(url: str) -> str | None:
    url = html_lib.unescape((url or "").strip())
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return None
    p = urlparse(url)
    if not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}{p.path or '/'}".rstrip("/")


def is_blocked_host(url: str) -> bool:
    host = host_of(url)
    return not host or any(h == host or host.endswith("." + h) for h in BLOCKED_HOST_HINTS)


class SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._is_result = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        d = dict(attrs)
        classes = d.get("class", "")
        self._is_result = "result__a" in classes
        self._href = d.get("href") if self._is_result else None
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.results.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []
            self._is_result = False


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg"}:
            self._ignore_depth += 1
            return
        if low == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg"} and self._ignore_depth:
            self._ignore_depth -= 1
            return
        if low == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._link_text).strip()))
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._ignore_depth:
            return
        value = " ".join(data.split())
        if value:
            self.text.append(value)
            if self._href is not None:
                self._link_text.append(value)


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str = ""
    source_query: str = ""


@dataclass(frozen=True)
class PageData:
    url: str
    text: str
    email: str | None
    contact_form_url: str | None
    links: tuple[str, ...]


def decode_ddg_href(href: str) -> str | None:
    href = html_lib.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if "duckduckgo.com" in p.netloc and p.path.startswith("/l/"):
        target = parse_qs(p.query).get("uddg", [None])[0]
        if target:
            return normalize_url(unquote(target))
    return normalize_url(href)


def search_web(session: requests.Session, query: str, max_results: int = 20) -> list[Candidate]:
    res = session.post(DDG_HTML_URL, data={"q": query}, timeout=20)
    res.raise_for_status()
    parser = SearchResultParser()
    parser.feed(res.text)
    out: list[Candidate] = []
    seen_hosts: set[str] = set()
    for href, title in parser.results:
        url = decode_ddg_href(href)
        if not url or is_blocked_host(url):
            continue
        host = host_of(url)
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        out.append(Candidate(url=url, title=title, source_query=query))
        if len(out) >= max_results:
            break
    return out


def extract_email(text: str) -> str | None:
    for raw in EMAIL_RE.findall(text or ""):
        value = raw.strip(".,;:()[]{}<>\"'")
        if valid_email(value):
            return value
    return None


def choose_contact(base_url: str, links: list[tuple[str, str]]) -> str | None:
    base_host = host_of(base_url)
    positives: list[str] = []
    for href, text in links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href)
        if host_of(full) != base_host or is_portal(full):
            continue
        low = f"{full} {text}".lower()
        if looks_booking_only(full) or any(k.lower() in low for k in NEGATIVE_CONTACT_HINTS):
            continue
        if any(k.lower() in low for k in CONTACT_HINTS):
            positives.append(full)
    return positives[0] if positives else None


def fetch_page(session: requests.Session, url: str, max_chars: int = 16000) -> PageData | None:
    try:
        res = session.get(url, timeout=15, allow_redirects=True)
        res.raise_for_status()
    except requests.RequestException:
        return None
    content_type = (res.headers.get("content-type") or "").lower()
    if content_type and "html" not in content_type:
        return None
    parser = PageParser()
    try:
        parser.feed(res.text)
    except Exception:
        return None
    text = "\n".join(parser.text)
    email = extract_email(res.text)
    form_url = choose_contact(res.url, parser.links)
    internal_links: list[str] = []
    seen: set[str] = set()
    for href, _ in parser.links:
        full = normalize_url(urljoin(res.url, href)) if href else None
        if full and host_of(full) == host_of(res.url) and full not in seen:
            seen.add(full)
            internal_links.append(full)
            if len(internal_links) >= 30:
                break
    return PageData(
        url=res.url,
        text=text[:max_chars],
        email=email,
        contact_form_url=form_url,
        links=tuple(internal_links),
    )


def ollama_prompt(page: PageData, area: str, category: str) -> str:
    link_text = "\n".join(page.links[:20])
    return f"""You are a Japanese business lead extraction engine for Path-Flow.
Return JSON only. Do not invent missing facts; use null or [] when unknown.
Target area: {area or 'unknown'}
Target category: {category or 'unknown'}
Website: {page.url}
Detected email: {page.email or 'unknown'}
Detected business contact form: {page.contact_form_url or 'unknown'}

Required JSON schema:
{{
  "company_name": string|null,
  "business_type": string|null,
  "area": string|null,
  "is_target": boolean,
  "confidence": number,
  "phone": string|null,
  "email": string|null,
  "contact_form_url": string|null,
  "services": [string],
  "price_range": string|null,
  "target_customer": string|null,
  "strengths": [string],
  "features": [string],
  "current_message": string|null,
  "lp_opportunities": [string]
}}

Internal links:
{link_text}

Page text:
{page.text}
"""


def call_ollama(
    session: requests.Session,
    page: PageData,
    area: str,
    category: str,
    model: str,
    ollama_url: str,
) -> dict:
    endpoint = ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": ollama_prompt(page, area, category),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    res = session.post(endpoint, json=payload, timeout=120)
    res.raise_for_status()
    body = res.json()
    raw = body.get("response") or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Ollama response is not a JSON object")
    return data


def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    if "legacy_email" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN legacy_email TEXT")
    if "legacy_form_url" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN legacy_form_url TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_discovery_intelligence (
          lead_id INTEGER PRIMARY KEY,
          source_query TEXT,
          source_url TEXT NOT NULL,
          model TEXT NOT NULL,
          confidence REAL,
          intelligence_json TEXT NOT NULL,
          discovery_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_discovery_source_url
          ON lead_discovery_intelligence(source_url);
        """
    )


def existing_hosts(conn: sqlite3.Connection) -> set[str]:
    return {
        host_of(r[0])
        for r in conn.execute("SELECT website_url FROM leads WHERE website_url IS NOT NULL AND website_url != ''")
        if host_of(r[0])
    }


def synthetic_place_id(url: str) -> str:
    return "web:" + hashlib.sha1(host_of(url).encode("utf-8")).hexdigest()


def insert_lead(
    conn: sqlite3.Connection,
    page: PageData,
    intel: dict,
    area: str,
    category: str,
    source_query: str,
    model: str,
) -> int | None:
    ensure_schema(conn)
    company_name = clean(intel.get("company_name")) or host_of(page.url)
    email = clean(intel.get("email"))
    if not valid_email(email):
        email = page.email if valid_email(page.email) else None
    form_url = clean(intel.get("contact_form_url")) or page.contact_form_url
    if form_url and (is_portal(form_url) or looks_booking_only(form_url)):
        form_url = page.contact_form_url
    phone = clean(intel.get("phone"))
    source_area = clean(intel.get("area")) or area
    source_category = clean(intel.get("business_type")) or category
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    data = {
        "place_id": synthetic_place_id(page.url),
        "company_name": company_name,
        "category": source_category,
        "area": source_area,
        "address": "",
        "phone": phone or "",
        "website_url": page.url,
        "rating": None,
        "user_ratings_total": None,
        "lifecycle_status": "CANDIDATE",
        "source": VERSION,
        "created_at": now_iso(),
        "legacy_email": email,
        "legacy_form_url": form_url,
    }
    usable = {k: v for k, v in data.items() if k in cols}
    names = list(usable)
    before = conn.total_changes
    conn.execute(
        f"INSERT OR IGNORE INTO leads ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        [usable[k] for k in names],
    )
    if conn.total_changes == before:
        return None
    row = conn.execute("SELECT lead_id FROM leads WHERE place_id=?", (data["place_id"],)).fetchone()
    if not row:
        return None
    lead_id = int(row[0])
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO lead_discovery_intelligence(
          lead_id, source_query, source_url, model, confidence,
          intelligence_json, discovery_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lead_id) DO UPDATE SET
          source_query=excluded.source_query,
          source_url=excluded.source_url,
          model=excluded.model,
          confidence=excluded.confidence,
          intelligence_json=excluded.intelligence_json,
          discovery_version=excluded.discovery_version,
          updated_at=excluded.updated_at
        """,
        (
            lead_id, source_query, page.url, model,
            float(intel.get("confidence") or 0),
            json.dumps(intel, ensure_ascii=False), VERSION, ts, ts,
        ),
    )
    return lead_id


def discover(
    db: Path,
    queries: list[str],
    seed_urls: list[str],
    area: str,
    category: str,
    model: str,
    ollama_url: str,
    max_results: int,
    apply: bool,
) -> dict:
    conn = sqlite3.connect(db)
    ensure_schema(conn)
    web = requests.Session()
    web.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NexccessLeadDiscovery/1.0)"})
    ollama = requests.Session()
    counts = {"candidate_url": 0, "existing": 0, "fetch_failed": 0, "ollama_failed": 0, "not_target": 0, "ready": 0, "inserted": 0}
    rows: list[dict] = []
    try:
        known_hosts = existing_hosts(conn)
        candidates: list[Candidate] = []
        seen_urls: set[str] = set()
        for q in queries:
            for c in search_web(web, q, max_results=max_results):
                if c.url not in seen_urls:
                    seen_urls.add(c.url)
                    candidates.append(c)
        for raw in seed_urls:
            url = normalize_url(raw)
            if url and not is_blocked_host(url) and url not in seen_urls:
                seen_urls.add(url)
                candidates.append(Candidate(url=url, title="seed", source_query="seed"))

        for candidate in candidates:
            counts["candidate_url"] += 1
            if host_of(candidate.url) in known_hosts:
                counts["existing"] += 1
                rows.append({"url": candidate.url, "status": "EXISTING"})
                continue
            page = fetch_page(web, candidate.url)
            if not page:
                counts["fetch_failed"] += 1
                rows.append({"url": candidate.url, "status": "FETCH_FAILED"})
                continue
            try:
                intel = call_ollama(ollama, page, area, category, model, ollama_url)
            except Exception as exc:
                counts["ollama_failed"] += 1
                rows.append({"url": page.url, "status": "OLLAMA_FAILED", "error": str(exc)[:160]})
                continue
            if intel.get("is_target") is False:
                counts["not_target"] += 1
                rows.append({"url": page.url, "status": "NOT_TARGET", "company_name": intel.get("company_name")})
                continue
            counts["ready"] += 1
            item = {
                "url": page.url,
                "status": "READY",
                "company_name": intel.get("company_name"),
                "confidence": intel.get("confidence"),
                "email": intel.get("email") or page.email,
                "form_url": intel.get("contact_form_url") or page.contact_form_url,
            }
            if apply:
                lead_id = insert_lead(conn, page, intel, area, category, candidate.source_query, model)
                if lead_id is not None:
                    counts["inserted"] += 1
                    known_hosts.add(host_of(page.url))
                    item["lead_id"] = lead_id
            rows.append(item)
        if apply:
            conn.commit()
        return {
            "version": VERSION,
            "db": str(db),
            "applied": apply,
            "model": model,
            "counts": counts,
            "results": rows,
        }
    finally:
        web.close()
        ollama.close()
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Low-cost Web -> Ollama -> Lead discovery for Path-Flow.")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--seed-url", action="append", default=[])
    p.add_argument("--area", default="")
    p.add_argument("--category", default="")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")
    if not args.query and not args.seed_url:
        raise SystemExit("At least one --query or --seed-url is required")
    result = discover(
        db=args.db,
        queries=args.query,
        seed_urls=args.seed_url,
        area=args.area,
        category=args.category,
        model=args.model,
        ollama_url=args.ollama_url,
        max_results=max(1, args.max_results),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

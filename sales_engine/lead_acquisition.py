from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from screening_automation import is_portal, looks_booking_only, valid_email

VERSION = "lead-acquisition-v1"
SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DEFAULT_AREAS = ["横浜駅", "川崎駅", "千葉駅", "船橋駅", "浦和駅", "大宮駅"]
DEFAULT_CATEGORIES = [
    "美容室", "理容室", "ネイルサロン", "まつ毛サロン", "眉毛サロン",
    "エステサロン", "痩身サロン", "脱毛サロン", "整体院", "接骨院",
    "鍼灸院", "パーソナルジム", "ピラティススタジオ", "ヨガスタジオ",
    "フォトスタジオ", "ペットサロン",
]

CONTACT_POSITIVE = (
    "contact", "inquiry", "otoiawase", "お問い合わせ", "問い合わせ", "お問合せ",
    "ご相談", "法人", "business", "company-contact",
)
CONTACT_NEGATIVE = (
    "reserve", "reservation", "booking", "book-online", "予約", "来店予約",
    "採用", "recruit", "求人", "support", "faq",
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        data = dict(attrs)
        self._href = data.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


@dataclass(frozen=True)
class ContactDiscovery:
    email: str | None = None
    form_url: str | None = None
    form_type: str | None = None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_contact_columns(conn: sqlite3.Connection) -> None:
    cols = table_columns(conn, "leads")
    if "legacy_email" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN legacy_email TEXT")
    if "legacy_form_url" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN legacy_form_url TEXT")


def existing_place_ids(conn: sqlite3.Connection) -> set[str]:
    cols = table_columns(conn, "leads")
    if "place_id" not in cols:
        raise SystemExit("leads table requires place_id")
    return {str(r[0]) for r in conn.execute("SELECT place_id FROM leads WHERE place_id IS NOT NULL AND place_id != ''")}


def extract_valid_email(text: str) -> str | None:
    for raw in EMAIL_RE.findall(html_lib.unescape(text or "")):
        candidate = raw.strip(".,;:()[]{}<>\"'")
        if valid_email(candidate):
            return candidate
    return None


def discover_contact(session: requests.Session, website_url: str, timeout: int = 10) -> ContactDiscovery:
    if not website_url or not website_url.startswith(("http://", "https://")) or is_portal(website_url):
        return ContactDiscovery()
    try:
        res = session.get(website_url, timeout=timeout, allow_redirects=True)
        res.raise_for_status()
    except requests.RequestException:
        return ContactDiscovery()

    email = extract_valid_email(res.text)
    parser = LinkParser()
    try:
        parser.feed(res.text)
    except Exception:
        pass

    base_host = urlparse(res.url).netloc.lower()
    positive: list[str] = []
    ambiguous: list[str] = []

    for href, text in parser.links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(res.url, href)
        low = f"{full} {text}".lower()
        host = urlparse(full).netloc.lower()
        if not host or host != base_host or is_portal(full):
            continue
        if any(k.lower() in low for k in CONTACT_NEGATIVE) or looks_booking_only(full):
            ambiguous.append(full)
            continue
        if any(k.lower() in low for k in CONTACT_POSITIVE):
            positive.append(full)

    if positive:
        form_url = positive[0]
        if not email:
            try:
                f_res = session.get(form_url, timeout=timeout, allow_redirects=True)
                f_res.raise_for_status()
                email = extract_valid_email(f_res.text)
            except requests.RequestException:
                pass
        return ContactDiscovery(email=email, form_url=form_url, form_type="CONTACT")

    if ambiguous:
        return ContactDiscovery(email=email, form_url=ambiguous[0], form_type="AMBIGUOUS")

    return ContactDiscovery(email=email)


def search_places(session: requests.Session, api_key: str, query: str, max_pages: int = 3) -> list[dict]:
    params = {"query": query, "key": api_key, "language": "ja"}
    results: list[dict] = []
    page = 0
    token_retries = 0
    while page < max_pages:
        res = session.get(SEARCH_URL, params=params, timeout=20)
        res.raise_for_status()
        payload = res.json()
        status = payload.get("status")
        if status == "OK":
            results.extend(payload.get("results", []))
            page += 1
            token = payload.get("next_page_token")
            if not token or page >= max_pages:
                break
            time.sleep(3)
            params = {"pagetoken": token, "key": api_key, "language": "ja"}
            token_retries = 0
            continue
        if status == "INVALID_REQUEST" and "pagetoken" in params and token_retries < 3:
            token_retries += 1
            time.sleep(3)
            continue
        if status == "ZERO_RESULTS":
            break
        raise RuntimeError(f"Places Text Search failed: query={query!r} status={status} error={payload.get('error_message')}")
    return results


def get_place_details(session: requests.Session, api_key: str, place_id: str) -> dict:
    params = {
        "place_id": place_id,
        "fields": "formatted_phone_number,website,formatted_address",
        "key": api_key,
        "language": "ja",
    }
    res = session.get(DETAILS_URL, params=params, timeout=20)
    res.raise_for_status()
    payload = res.json()
    if payload.get("status") == "OK":
        return payload.get("result", {})
    return {}


def insert_candidate(conn: sqlite3.Connection, data: dict) -> int | None:
    cols = table_columns(conn, "leads")
    usable = {k: v for k, v in data.items() if k in cols}
    if "place_id" not in usable or "company_name" not in usable:
        raise RuntimeError("leads schema does not support required place_id/company_name fields")
    names = list(usable)
    sql = f"INSERT OR IGNORE INTO leads ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
    before = conn.total_changes
    conn.execute(sql, [usable[k] for k in names])
    if conn.total_changes == before:
        return None
    row = conn.execute("SELECT lead_id FROM leads WHERE place_id=?", (usable["place_id"],)).fetchone()
    return int(row[0]) if row else None


def update_contact(conn: sqlite3.Connection, lead_id: int, contact: ContactDiscovery) -> None:
    ensure_contact_columns(conn)
    conn.execute(
        "UPDATE leads SET legacy_email=?, legacy_form_url=? WHERE lead_id=?",
        (contact.email, contact.form_url, lead_id),
    )


def collect(
    db: Path,
    api_key: str,
    areas: list[str],
    categories: list[str],
    min_rating: float,
    max_pages: int,
    apply: bool,
    discover_contacts: bool,
    sleep_seconds: float,
) -> dict:
    conn = sqlite3.connect(db)
    session = requests.Session()
    session.headers.update({"User-Agent": "Nexccess-LeadAcquisition/1.0"})
    counts = Counter()
    queries: list[dict] = []
    try:
        known = existing_place_ids(conn)
        for area in areas:
            for category in categories:
                query = f"{area} {category}"
                places = search_places(session, api_key, query, max_pages=max_pages)
                q = Counter(found=len(places))
                seen_this_query: set[str] = set()
                for place in places:
                    place_id = str(place.get("place_id") or "").strip()
                    if not place_id or place_id in seen_this_query:
                        q["invalid_or_duplicate_result"] += 1
                        continue
                    seen_this_query.add(place_id)
                    rating = float(place.get("rating") or 0)
                    if rating < min_rating:
                        q["below_rating"] += 1
                        continue
                    if place_id in known:
                        q["existing"] += 1
                        counts["existing"] += 1
                        continue
                    q["new_candidate"] += 1
                    counts["new_candidate"] += 1
                    if not apply:
                        continue

                    details = get_place_details(session, api_key, place_id)
                    data = {
                        "place_id": place_id,
                        "company_name": place.get("name") or "",
                        "category": category,
                        "area": area,
                        "address": details.get("formatted_address") or place.get("formatted_address") or "",
                        "phone": details.get("formatted_phone_number") or "",
                        "website_url": details.get("website") or "",
                        "rating": rating,
                        "user_ratings_total": int(place.get("user_ratings_total") or 0),
                        "lifecycle_status": "CANDIDATE",
                        "source": VERSION,
                        "created_at": now_iso(),
                    }
                    lead_id = insert_candidate(conn, data)
                    if lead_id is None:
                        q["race_duplicate"] += 1
                        continue
                    known.add(place_id)
                    q["inserted"] += 1
                    counts["inserted"] += 1

                    if discover_contacts and data["website_url"]:
                        contact = discover_contact(session, data["website_url"])
                        update_contact(conn, lead_id, contact)
                        if contact.email:
                            counts["email_found"] += 1
                        if contact.form_url:
                            counts["form_found"] += 1
                        if contact.form_type == "AMBIGUOUS":
                            counts["ambiguous_form"] += 1
                    if sleep_seconds:
                        time.sleep(sleep_seconds)

                queries.append({"query": query, **dict(q)})
                if apply:
                    conn.commit()

        return {
            "version": VERSION,
            "db": str(db),
            "applied": apply,
            "discover_contacts": discover_contacts,
            "areas": areas,
            "categories": categories,
            "summary": dict(counts),
            "queries": queries,
        }
    finally:
        session.close()
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Lead Acquisition v1: Google candidate discovery with dedupe and optional contact discovery.")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--area", action="append", default=[])
    p.add_argument("--category", action="append", default=[])
    p.add_argument("--min-rating", type=float, default=2.0)
    p.add_argument("--max-pages", type=int, default=1, choices=(1, 2, 3))
    p.add_argument("--discover-contacts", action="store_true")
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--apply", action="store_true", help="Write new candidates. Default is discovery-only dry-run.")
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")

    result = collect(
        db=args.db,
        api_key=api_key,
        areas=args.area or DEFAULT_AREAS,
        categories=args.category or DEFAULT_CATEGORIES,
        min_rating=args.min_rating,
        max_pages=args.max_pages,
        apply=args.apply,
        discover_contacts=args.discover_contacts,
        sleep_seconds=max(0.0, args.sleep),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

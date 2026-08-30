from __future__ import annotations

import json
import sqlite3

import lead_discovery as base
from screening_automation import is_portal, looks_booking_only, valid_email

# Match the model actually installed on the local Nexccess Ollama host.
base.DEFAULT_MODEL = "hf.co/Qwen/Qwen2.5-7B-Instruct-GGUF:latest"

# Deterministic exclusions for obvious directory/marketplace sources. These are
# useful discovery references, but are not the store/company targets we want to
# enqueue for Path-Flow sales.
base.BLOCKED_HOST_HINTS = base.BLOCKED_HOST_HINTS + (
    "beauty.rakuten.co.jp",
    "beauty-park.jp",
    "nailie.jp",
    "machi-biz.com",
    "beautifyjp.net",
    "minimodel.jp",
    "nailbook.jp",
)

# Do not mistake recruiting, school/college, document-request, or reservation
# routes for a business sales-contact route.
base.NEGATIVE_CONTACT_HINTS = base.NEGATIVE_CONTACT_HINTS + (
    "career", "job", "college", "school", "request", "資料請求", "スクール", "学校",
)


# --- Runtime web enrichment -------------------------------------------------
# v1 only read the first page. For sales contactability, follow a few same-host
# links that are likely to contain business/contact information.
_original_fetch_page = base.fetch_page


def _contact_link_score(url: str) -> int:
    low = (url or "").lower()
    if any(k in low for k in (
        "recruit", "career", "job", "求人", "採用", "college", "school", "request",
        "資料請求", "reserve", "reservation", "booking", "予約",
    )):
        return 0
    if any(k in low for k in ("contact", "inquiry", "otoiawase", "toiawase")):
        return 100
    if any(k in low for k in ("company", "corporate", "about", "profile")):
        return 70
    if any(k in low for k in ("salon", "shop", "store", "access")):
        return 40
    return 0


def fetch_page_deep(session, url: str, max_chars: int = 16000):
    root = _original_fetch_page(session, url, max_chars=max_chars)
    if not root:
        return None

    # If the top page already exposes a usable route, avoid extra requests.
    if valid_email(root.email) or root.contact_form_url:
        return root

    candidates = sorted(
        ((u, _contact_link_score(u)) for u in root.links),
        key=lambda x: x[1],
        reverse=True,
    )
    candidates = [u for u, score in candidates if score > 0][:4]

    merged_text = [root.text]
    email = root.email
    form_url = root.contact_form_url
    merged_links = list(root.links)

    for child_url in candidates:
        child = _original_fetch_page(session, child_url, max_chars=8000)
        if not child:
            continue
        print(f"[DEEP] {base.host_of(root.url)} -> {child_url}", flush=True)
        if child.text:
            merged_text.append(child.text)
        if not email and valid_email(child.email):
            email = child.email
        if not form_url and child.contact_form_url:
            form_url = child.contact_form_url
        for link in child.links:
            if link not in merged_links:
                merged_links.append(link)
        if email or form_url:
            break

    return base.PageData(
        url=root.url,
        text="\n".join(merged_text)[:24000],
        email=email,
        contact_form_url=form_url,
        links=tuple(merged_links[:40]),
    )


base.fetch_page = fetch_page_deep


# --- Observable/retrying local Ollama ---------------------------------------
_original_call_ollama = base.call_ollama
_progress_counter = 0


def call_ollama_with_progress(session, page, area, category, model, ollama_url):
    global _progress_counter
    _progress_counter += 1
    n = _progress_counter
    print(f"[OLLAMA {n}] {base.host_of(page.url)} ...", flush=True)
    try:
        data = _original_call_ollama(session, page, area, category, model, ollama_url)
    except Exception as exc:
        # One retry with shorter context handles occasional 120s local-model stalls
        # without hiding a persistent failure.
        print(f"[OLLAMA {n}] RETRY  {type(exc).__name__}: {str(exc)[:100]}", flush=True)
        compact = base.PageData(
            url=page.url,
            text=page.text[:8000],
            email=page.email,
            contact_form_url=page.contact_form_url,
            links=page.links[:20],
        )
        data = _original_call_ollama(session, compact, area, category, model, ollama_url)

    name = base.clean(data.get("company_name")) or "(name unknown)"
    confidence = data.get("confidence")
    print(f"[OLLAMA {n}] OK  {name}  confidence={confidence}", flush=True)
    return data


base.call_ollama = call_ollama_with_progress


def insert_lead_compatible(
    conn: sqlite3.Connection,
    page: base.PageData,
    intel: dict,
    area: str,
    category: str,
    source_query: str,
    model: str,
) -> int | None:
    """Insert into the real lead_intelligence schema."""
    base.ensure_schema(conn)
    company_name = base.clean(intel.get("company_name")) or base.host_of(page.url)

    email = base.clean(intel.get("email"))
    if not valid_email(email):
        email = page.email if valid_email(page.email) else None

    form_url = base.clean(intel.get("contact_form_url")) or page.contact_form_url
    if form_url and (is_portal(form_url) or looks_booking_only(form_url)):
        form_url = page.contact_form_url
    if form_url:
        low_form = form_url.lower()
        if any(k in low_form for k in (
            "recruit", "career", "job", "college", "school", "request", "reserve",
            "reservation", "booking",
        )):
            form_url = None

    phone = base.clean(intel.get("phone"))
    source_area = base.clean(intel.get("area")) or area
    source_category = base.clean(intel.get("business_type")) or category
    ts = base.now_iso()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    data = {
        "place_id": base.synthetic_place_id(page.url),
        "company_name": company_name,
        "category": source_category,
        "area": source_area,
        "address": "",
        "phone": phone or "",
        "website_url": page.url,
        "rating": None,
        "user_ratings_total": None,
        "lifecycle_status": "CANDIDATE",
        "source": base.VERSION,
        "source_record_id": base.host_of(page.url),
        "collected_at": ts,
        "imported_at": ts,
        "legacy_email": email,
        "legacy_form_url": form_url,
    }
    usable = {k: v for k, v in data.items() if k in cols}
    names = list(usable)

    conn.execute(
        f"INSERT INTO leads ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)}) "
        "ON CONFLICT(place_id) DO NOTHING",
        [usable[k] for k in names],
    )

    row = conn.execute(
        "SELECT lead_id FROM leads WHERE place_id=?",
        (data["place_id"],),
    ).fetchone()
    if not row:
        return None

    lead_id = int(row[0])
    existing_intel = conn.execute(
        "SELECT 1 FROM lead_discovery_intelligence WHERE lead_id=?",
        (lead_id,),
    ).fetchone()
    if existing_intel:
        return None

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
            lead_id,
            source_query,
            page.url,
            model,
            float(intel.get("confidence") or 0),
            json.dumps(intel, ensure_ascii=False),
            base.VERSION,
            ts,
            ts,
        ),
    )
    print(f"[DB] INSERT lead_id={lead_id}  {company_name}", flush=True)
    return lead_id


base.insert_lead = insert_lead_compatible


if __name__ == "__main__":
    base.main()

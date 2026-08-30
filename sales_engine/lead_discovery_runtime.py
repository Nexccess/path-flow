from __future__ import annotations

import json
import sqlite3

import lead_discovery as base
from screening_automation import is_portal, looks_booking_only, valid_email

# Match the model actually installed on the local Nexccess Ollama host.
base.DEFAULT_MODEL = "hf.co/Qwen/Qwen2.5-7B-Instruct-GGUF:latest"


def insert_lead_compatible(
    conn: sqlite3.Connection,
    page: base.PageData,
    intel: dict,
    area: str,
    category: str,
    source_query: str,
    model: str,
) -> int | None:
    """Insert into the real lead_intelligence schema.

    The production leads table requires imported_at NOT NULL and does not have
    the created_at column used by the first Lead Discovery draft.
    """
    base.ensure_schema(conn)
    company_name = base.clean(intel.get("company_name")) or base.host_of(page.url)

    email = base.clean(intel.get("email"))
    if not valid_email(email):
        email = page.email if valid_email(page.email) else None

    form_url = base.clean(intel.get("contact_form_url")) or page.contact_form_url
    if form_url and (is_portal(form_url) or looks_booking_only(form_url)):
        form_url = page.contact_form_url

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

    # Ignore only a duplicate place_id. Other schema errors must surface.
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
    return lead_id


# Patch the draft implementation at runtime; discover() resolves this global.
base.insert_lead = insert_lead_compatible


if __name__ == "__main__":
    base.main()

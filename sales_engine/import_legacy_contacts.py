from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def normalize(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("legacy_db", type=Path, help="Existing C:/Nexcess/leads_database.db")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    args = p.parse_args()

    if not args.legacy_db.exists():
        raise SystemExit(f"Legacy DB not found: {args.legacy_db}")

    target = sqlite3.connect(args.db)
    legacy = sqlite3.connect(args.legacy_db)
    try:
        legacy_cols = {row[1] for row in legacy.execute("PRAGMA table_info(leads)")}
        required = {"place_id", "form_url", "email"}
        missing = required - legacy_cols
        if missing:
            raise SystemExit(f"Legacy leads table missing columns: {sorted(missing)}")

        old_rows = {
            normalize(place_id): (normalize(form_url), normalize(email), normalize(website_url))
            for place_id, form_url, email, website_url in legacy.execute(
                "SELECT place_id, form_url, email, website_url FROM leads WHERE place_id IS NOT NULL"
            )
            if normalize(place_id)
        }

        rows = target.execute(
            "SELECT store_id, google_place_id FROM leads WHERE campaign_id=?",
            (CAMPAIGN_ID,),
        ).fetchall()

        matched = 0
        usable = 0
        email_count = 0
        form_count = 0
        for store_id, place_id in rows:
            place_id = normalize(place_id)
            if not place_id or place_id not in old_rows:
                continue
            matched += 1
            form_url, email, website_url = old_rows[place_id]
            if form_url and "hotpepper.jp" in form_url.lower():
                form_url = None

            if email:
                status = "READY_EMAIL"
                channel = "email"
                allowed = 1
                confidence = "HIGH"
                email_count += 1
            elif form_url:
                status = "READY_FORM"
                channel = "form"
                allowed = 0
                confidence = "HIGH"
                form_count += 1
            else:
                status = "LEGACY_NO_CONTACT"
                channel = None
                allowed = 0
                confidence = "HIGH"

            if email or form_url:
                usable += 1

            target.execute(
                """
                UPDATE leads SET
                  contact_status=?, primary_channel=?, email=?, contact_form_url=?,
                  contact_source_url=COALESCE(?, contact_source_url), contact_checked_at=?,
                  contact_confidence=?, send_allowed=?, screening_status=?, updated_at=?
                WHERE campaign_id=? AND store_id=?
                """,
                (
                    status, channel, email, form_url, website_url, now_iso(), confidence, allowed,
                    "READY" if allowed else ("FORM_READY" if form_url else "REVIEW"),
                    now_iso(), CAMPAIGN_ID, store_id,
                ),
            )

        target.commit()
        print(
            f"legacy_match={matched} usable_contacts={usable} "
            f"email={email_count} form_only={form_count} total_campaign={len(rows)}"
        )
    finally:
        legacy.close()
        target.close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from form_preflight import CAMPAIGN_ID, FIELD_HINTS, field_matches, inspect_form
from templates import initial_body, subject

SENDER_NAME = "合同会社Nexccess"
SENDER_EMAIL = "info@nexccess.com"

SUBJECT_HINTS = ("subject", "title", "件名", "題名")
PHONE_HINTS = ("phone", "tel", "電話")
CONSENT_HINTS = ("agree", "consent", "privacy", "policy", "同意", "個人情報")


def classify_field(field: dict) -> str | None:
    field_type = str(field.get("type", "")).lower()
    if field_type == "hidden":
        return "hidden"
    if field_matches(field, FIELD_HINTS["email"]):
        return "email"
    if field_matches(field, FIELD_HINTS["message"]) or field.get("tag") == "textarea":
        return "message"
    if field_matches(field, FIELD_HINTS["name"]):
        return "name"
    if field_matches(field, SUBJECT_HINTS):
        return "subject"
    if field_matches(field, PHONE_HINTS):
        return "phone"
    return None


def build_plan(store_id: str, store_name: str, lp_url: str, form_url: str) -> dict:
    audit = inspect_form(form_url)
    if audit.get("decision") != "AUTO_READY":
        return {
            "decision": "BLOCKED_PREFLIGHT",
            "store_id": store_id,
            "store_name": store_name,
            "form_url": form_url,
            "reason": audit.get("reason", "preflight failed"),
        }

    selected = audit.get("selected_form") or {}
    payload: dict[str, str] = {}
    unknown_required: list[dict] = []
    mapped: list[dict] = []

    mail_subject = subject(store_name, store_id)
    body = initial_body(store_name, store_id, lp_url)

    for field in selected.get("fields", []):
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        role = classify_field(field)
        field_type = str(field.get("type", "")).lower()
        required = bool(field.get("required"))

        if role == "hidden":
            payload[name] = str(field.get("value") or "")
            mapped.append({"name": name, "role": "hidden"})
        elif role == "email":
            payload[name] = SENDER_EMAIL
            mapped.append({"name": name, "role": "email"})
        elif role == "message":
            payload[name] = body
            mapped.append({"name": name, "role": "message"})
        elif role == "name":
            payload[name] = SENDER_NAME
            mapped.append({"name": name, "role": "name"})
        elif role == "subject":
            payload[name] = mail_subject
            mapped.append({"name": name, "role": "subject"})
        elif role == "phone":
            if required:
                unknown_required.append({"name": name, "type": field_type, "reason": "required phone field has no configured sender phone"})
        elif required:
            hay = " ".join(str(field.get(k, "")) for k in ("name", "id", "placeholder")).lower()
            if field_type in {"checkbox", "radio"} or any(h in hay for h in CONSENT_HINTS):
                reason = "required consent/choice field requires explicit semantics"
            else:
                reason = "unmapped required field"
            unknown_required.append({"name": name, "type": field_type, "reason": reason})

    roles = {x["role"] for x in mapped}
    if "email" not in roles or "message" not in roles:
        return {
            "decision": "BLOCKED_MAPPING",
            "store_id": store_id,
            "store_name": store_name,
            "form_url": form_url,
            "reason": "email/message field mapping incomplete",
            "mapped": mapped,
        }

    if unknown_required:
        return {
            "decision": "BLOCKED_REQUIRED_FIELD",
            "store_id": store_id,
            "store_name": store_name,
            "form_url": form_url,
            "reason": "required fields remain unresolved",
            "unknown_required": unknown_required,
            "mapped": mapped,
        }

    return {
        "decision": "SEND_READY",
        "store_id": store_id,
        "store_name": store_name,
        "form_url": form_url,
        "action": selected.get("action"),
        "method": selected.get("method"),
        "mapped": mapped,
        "payload_field_names": sorted(payload.keys()),
        # Intentionally do not print the full message/payload in dry-run logs.
    }


def run(db: Path, limit: int | None = None) -> dict:
    conn = sqlite3.connect(db)
    counts: dict[str, int] = {}
    try:
        sql = """
            SELECT store_id, store_name, lp_url, contact_form_url
            FROM leads
            WHERE campaign_id=?
              AND contact_status='READY_FORM'
              AND screening_status='FORM_AUTO_READY'
              AND send_allowed=1
              AND sales_status='READY'
              AND human_action=0
            ORDER BY store_id
        """
        params: list[object] = [CAMPAIGN_ID]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        for store_id, store_name, lp_url, form_url in rows:
            result = build_plan(store_id, store_name, lp_url, form_url)
            decision = result["decision"]
            counts[decision] = counts.get(decision, 0) + 1
            print(f"{decision}\t{store_id}\t{store_name}\t{form_url}")
            if result.get("reason"):
                print("reason=" + str(result["reason"]))
            if result.get("unknown_required"):
                print("unknown_required=" + json.dumps(result["unknown_required"], ensure_ascii=False))
            if result.get("mapped"):
                print("mapped=" + json.dumps(result["mapped"], ensure_ascii=False))

        summary = {"targets": len(rows), **counts, "submitted": 0}
        print("summary=" + json.dumps(summary, ensure_ascii=False))
        return summary
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Build safe submission plans for preflight-approved inquiry forms. No form is submitted.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    run(args.db, limit=args.limit)


if __name__ == "__main__":
    main()

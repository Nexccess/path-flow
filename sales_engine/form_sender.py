from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

from form_preflight import CAMPAIGN_ID, FIELD_HINTS, field_matches, inspect_form
from templates import initial_body, subject

SENDER_COMPANY = os.environ.get("PF_SENDER_COMPANY", "合同会社Nexccess").strip()
SENDER_EMAIL = os.environ.get("PF_SENDER_EMAIL", "info@nexccess.com").strip()
SENDER_PHONE = os.environ.get("PF_SENDER_PHONE", "").strip()
SENDER_LAST_NAME = os.environ.get("PF_SENDER_LAST_NAME", "").strip()
SENDER_FIRST_NAME = os.environ.get("PF_SENDER_FIRST_NAME", "").strip()
SENDER_LAST_NAME_KANA = os.environ.get("PF_SENDER_LAST_NAME_KANA", "").strip()
SENDER_FIRST_NAME_KANA = os.environ.get("PF_SENDER_FIRST_NAME_KANA", "").strip()

SUBJECT_HINTS = ("subject", "title", "件名", "題名")
PHONE_HINTS = ("phone", "tel", "電話")
CONSENT_HINTS = ("agree", "consent", "privacy", "policy", "同意", "個人情報")
RESERVATION_HINTS = (
    "第1希望", "第2希望", "第3希望", "希望日", "希望時間", "予約日", "予約時間",
    "preferred date", "preferred time", "reservation date", "reservation time",
)
IDENTITY_EXACT_NAMES = {
    "lastname", "firstname", "lastnamekana", "firstnamekana",
    "姓", "名", "セイ", "メイ", "お名前（姓）", "お名前（名）", "氏名（姓）", "氏名（名）",
    "姓カナ", "名カナ", "姓かな", "名かな", "salonname", "staffname",
}


def classify_field(field: dict) -> str | None:
    field_type = str(field.get("type", "")).lower()
    name = str(field.get("name") or "").strip()
    if field_type == "hidden":
        return "hidden"
    if field_matches(field, FIELD_HINTS["email"]):
        return "email"
    if field_matches(field, FIELD_HINTS["message"]) or field.get("tag") == "textarea":
        return "message"
    if field_matches(field, SUBJECT_HINTS):
        return "subject"
    if field_matches(field, PHONE_HINTS):
        return "phone"
    if name.lower() in {x.lower() for x in IDENTITY_EXACT_NAMES}:
        return "name"
    if field_matches(field, FIELD_HINTS["name"]):
        return "name"
    return None


def field_identity_value(field: dict) -> tuple[str | None, str | None]:
    name = str(field.get("name") or "").strip()
    key = name.lower()

    exact = {
        "lastname": (SENDER_LAST_NAME, "last_name"),
        "firstname": (SENDER_FIRST_NAME, "first_name"),
        "lastnamekana": (SENDER_LAST_NAME_KANA, "last_name_kana"),
        "firstnamekana": (SENDER_FIRST_NAME_KANA, "first_name_kana"),
        "salonname": (SENDER_COMPANY, "company"),
        "staffname": (SENDER_LAST_NAME + SENDER_FIRST_NAME if SENDER_LAST_NAME and SENDER_FIRST_NAME else "", "person_name"),
    }
    if key in exact:
        value, role = exact[key]
        return (value or None), role

    if name in {"姓", "お名前（姓）", "氏名（姓）"}:
        return (SENDER_LAST_NAME or None), "last_name"
    if name in {"名", "お名前（名）", "氏名（名）"}:
        return (SENDER_FIRST_NAME or None), "first_name"
    if name in {"セイ", "姓カナ", "姓かな"}:
        return (SENDER_LAST_NAME_KANA or None), "last_name_kana"
    if name in {"メイ", "名カナ", "名かな"}:
        return (SENDER_FIRST_NAME_KANA or None), "first_name_kana"

    return SENDER_COMPANY, "name"


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
        hay = " ".join(str(field.get(k, "")) for k in ("name", "id", "placeholder")).lower()

        if required and any(h.lower() in hay for h in RESERVATION_HINTS):
            unknown_required.append({"name": name, "type": field_type, "reason": "required reservation field is not appropriate for sales outreach"})
            continue

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
            value, identity_role = field_identity_value(field)
            if value:
                payload[name] = value
                mapped.append({"name": name, "role": identity_role})
            elif required:
                unknown_required.append({"name": name, "type": field_type, "reason": f"required identity field missing configuration: {identity_role}"})
        elif role == "subject":
            payload[name] = mail_subject
            mapped.append({"name": name, "role": "subject"})
        elif role == "phone":
            if SENDER_PHONE:
                payload[name] = SENDER_PHONE
                mapped.append({"name": name, "role": "phone"})
            elif required:
                unknown_required.append({"name": name, "type": field_type, "reason": "required phone field has no PF_SENDER_PHONE configuration"})
        elif required:
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

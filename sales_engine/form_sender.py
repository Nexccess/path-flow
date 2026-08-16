from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

from form_preflight import CAMPAIGN_ID, FIELD_HINTS, USER_AGENT, field_matches, inspect_form
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
HONEYPOT_HINTS = (
    "_wpcf7_ak_hp_", "honeypot", "honey-pot", "hp_textarea", "antispam", "anti-spam"
)
PHONE_SEGMENT_RE = re.compile(r"\[(?:data)?\]\[(\d+)\]$", re.I)
SUCCESS_HINTS = (
    "送信しました", "送信完了", "お問い合わせありがとうございました", "お問い合わせを受け付けました",
    "thank you", "thanks for contacting", "message has been sent", "successfully sent",
)
FAILURE_HINTS = (
    "入力してください", "必須項目", "入力内容をご確認", "エラー", "送信できません",
    "validation error", "failed to send", "there was an error", "invalid field",
)
SUBMISSION_LOG = ROOT_DIR / "logs" / "form_submission_attempts.jsonl"
TIMEOUT_SECONDS = 15


def is_honeypot_field(field: dict) -> bool:
    hay = " ".join(str(field.get(k, "")) for k in ("name", "id", "placeholder")).lower()
    return any(h in hay for h in HONEYPOT_HINTS)


def classify_field(field: dict) -> str | None:
    field_type = str(field.get("type", "")).lower()
    name = str(field.get("name") or "").strip()
    if is_honeypot_field(field):
        return "honeypot"
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


def phone_parts(phone: str) -> list[str]:
    raw_parts = [p for p in re.split(r"\D+", phone.strip()) if p]
    if len(raw_parts) == 3:
        return raw_parts

    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11:
        return [digits[:3], digits[3:7], digits[7:]]
    if len(digits) == 10:
        if digits.startswith(("03", "06")):
            return [digits[:2], digits[2:6], digits[6:]]
        return [digits[:3], digits[3:6], digits[6:]]
    return [digits] if digits else []


def phone_value_for_field(field_name: str) -> tuple[str | None, str]:
    if not SENDER_PHONE:
        return None, "phone"
    m = PHONE_SEGMENT_RE.search(field_name)
    if not m:
        return SENDER_PHONE, "phone"
    parts = phone_parts(SENDER_PHONE)
    idx = int(m.group(1))
    if len(parts) == 3 and 0 <= idx < 3:
        return parts[idx], f"phone_segment_{idx}"
    return None, f"phone_segment_{idx}"


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

        if role == "honeypot":
            payload[name] = ""
            mapped.append({"name": name, "role": "honeypot_blank"})
        elif role == "hidden":
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
            value, phone_role = phone_value_for_field(name)
            if value:
                payload[name] = value
                mapped.append({"name": name, "role": phone_role})
            elif required or PHONE_SEGMENT_RE.search(name):
                unknown_required.append({"name": name, "type": field_type, "reason": "phone field could not be safely mapped from PF_SENDER_PHONE"})
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
        "_payload": payload,
    }


def previous_submission_attempt(store_id: str) -> bool:
    if not SUBMISSION_LOG.exists():
        return False
    try:
        for line in SUBMISSION_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if str(item.get("store_id")) == str(store_id) and item.get("request_sent") is True:
                return True
    except Exception:
        return True
    return False


def append_submission_log(item: dict) -> None:
    SUBMISSION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SUBMISSION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def submit_plan(plan: dict) -> dict:
    store_id = str(plan["store_id"])
    if previous_submission_attempt(store_id):
        return {
            "decision": "BLOCKED_DUPLICATE_GUARD",
            "store_id": store_id,
            "reason": "A previous HTTP submission attempt is already logged for this store.",
        }

    action = str(plan.get("action") or "")
    form_url = str(plan.get("form_url") or "")
    payload = dict(plan.get("_payload") or {})
    if not action or not payload:
        return {"decision": "BLOCKED_NO_PAYLOAD", "store_id": store_id, "reason": "action/payload missing"}

    action_host = urlparse(action).netloc.lower()
    form_host = urlparse(form_url).netloc.lower()
    if not action_host or not form_host or not (
        action_host == form_host or action_host.endswith("." + form_host) or form_host.endswith("." + action_host)
    ):
        return {"decision": "BLOCKED_ACTION_HOST", "store_id": store_id, "reason": "form action host changed"}

    data = urlencode(payload).encode("utf-8")
    origin = f"{urlparse(form_url).scheme}://{form_host}"
    req = Request(
        action,
        data=data,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": form_url,
            "Origin": origin,
        },
    )

    log_item = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "campaign_id": CAMPAIGN_ID,
        "store_id": store_id,
        "store_name": plan.get("store_name"),
        "form_url": form_url,
        "action": action,
        "request_sent": False,
    }

    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            log_item["request_sent"] = True
            log_item["http_status"] = getattr(res, "status", None)
            log_item["final_url"] = res.geturl()
            raw = res.read(500_000)
            charset = res.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            low = re.sub(r"\s+", " ", text).lower()
            has_success = any(h.lower() in low for h in SUCCESS_HINTS)
            has_failure = any(h.lower() in low for h in FAILURE_HINTS)
            if has_success and not has_failure:
                decision = "SUBMITTED_CONFIRMED"
            elif has_failure:
                decision = "SUBMITTED_VALIDATION_FAILED"
            else:
                decision = "SUBMITTED_UNVERIFIED"
            log_item["decision"] = decision
            append_submission_log(log_item)
            return {
                "decision": decision,
                "store_id": store_id,
                "http_status": log_item["http_status"],
                "final_url": log_item["final_url"],
                "request_sent": True,
            }
    except HTTPError as exc:
        log_item["request_sent"] = True
        log_item["http_status"] = exc.code
        log_item["decision"] = "SUBMITTED_HTTP_ERROR"
        append_submission_log(log_item)
        return {"decision": "SUBMITTED_HTTP_ERROR", "store_id": store_id, "http_status": exc.code, "request_sent": True}
    except (URLError, TimeoutError, ValueError) as exc:
        log_item["decision"] = "SUBMISSION_TRANSPORT_FAILED"
        log_item["error"] = type(exc).__name__
        append_submission_log(log_item)
        return {"decision": "SUBMISSION_TRANSPORT_FAILED", "store_id": store_id, "request_sent": False, "reason": type(exc).__name__}


def run(
    db: Path,
    limit: int | None = None,
    store_id: str | None = None,
    live: bool = False,
    confirm_store_id: str | None = None,
) -> dict:
    if live and store_id:
        if confirm_store_id != store_id:
            raise SystemExit("--live requires --confirm-store-id with the same store ID")
        limit = 1

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
        """
        params: list[object] = [CAMPAIGN_ID]
        if store_id:
            sql += " AND store_id=?"
            params.append(store_id)
        sql += " ORDER BY store_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        if live and store_id and len(rows) != 1:
            raise SystemExit(f"Live test requires exactly one eligible row; found {len(rows)}")

        submitted = 0
        for row_store_id, store_name, lp_url, form_url in rows:
            result = build_plan(row_store_id, store_name, lp_url, form_url)
            decision = result["decision"]
            counts[decision] = counts.get(decision, 0) + 1
            print(f"{decision}\t{row_store_id}\t{store_name}\t{form_url}")
            if result.get("reason"):
                print("reason=" + str(result["reason"]))
            if result.get("unknown_required"):
                print("unknown_required=" + json.dumps(result["unknown_required"], ensure_ascii=False))
            if result.get("mapped"):
                print("mapped=" + json.dumps(result["mapped"], ensure_ascii=False))

            if live:
                if decision != "SEND_READY":
                    print("live_result=BLOCKED_NOT_SEND_READY")
                    continue
                live_result = submit_plan(result)
                print("live_result=" + json.dumps(live_result, ensure_ascii=False))
                live_decision = live_result["decision"]
                counts[live_decision] = counts.get(live_decision, 0) + 1
                if live_result.get("request_sent"):
                    submitted += 1

        summary = {"targets": len(rows), **counts, "submitted": submitted, "live": live}
        print("summary=" + json.dumps(summary, ensure_ascii=False))
        return summary
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Build safe inquiry-form submission plans. Live mode is restricted to one explicitly confirmed store.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--limit", type=int)
    p.add_argument("--store-id", type=str, help="Restrict processing to one store ID.")
    p.add_argument("--live", action="store_true", help="Actually submit exactly one eligible form. Requires --store-id and --confirm-store-id.")
    p.add_argument("--confirm-store-id", type=str, help="Explicit live-send confirmation; must equal --store-id.")
    args = p.parse_args()
    run(
        args.db,
        limit=args.limit,
        store_id=args.store_id,
        live=args.live,
        confirm_store_id=args.confirm_store_id,
    )


if __name__ == "__main__":
    main()

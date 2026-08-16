from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from form_preflight import CAMPAIGN_ID, USER_AGENT
from form_sender import (
    FAILURE_HINTS,
    SUCCESS_HINTS,
    append_submission_log,
    build_plan,
    previous_submission_attempt,
)

TIMEOUT_SECONDS = 15
CF7_HINTS = ("_wpcf7", "wpcf7-response-output", "wpcf7-form")


def _extract_cf7_response(text: str) -> str | None:
    patterns = (
        r'<div[^>]*class=["\'][^"\']*wpcf7-response-output[^"\']*["\'][^>]*>(.*?)</div>',
        r'<div[^>]*class=["\'][^"\']*screen-reader-response[^"\']*["\'][^>]*>(.*?)</div>',
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I | re.S)
        if m:
            value = re.sub(r"<[^>]+>", " ", m.group(1))
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                return value[:800]
    return None


def _analyze_response(text: str, final_url: str) -> dict:
    low = re.sub(r"\s+", " ", text).lower()
    success_matches = [h for h in SUCCESS_HINTS if h.lower() in low]
    failure_matches = [h for h in FAILURE_HINTS if h.lower() in low]
    cf7_detected = any(h.lower() in low for h in CF7_HINTS)
    cf7_response = _extract_cf7_response(text)
    fragment = urlparse(final_url).fragment

    if success_matches and not failure_matches:
        assessment = "CONFIRMED_SUCCESS_TEXT"
    elif failure_matches:
        assessment = "CONFIRMED_FAILURE_TEXT"
    elif cf7_detected and fragment.startswith("wpcf7-"):
        assessment = "CF7_NON_AJAX_RESPONSE_UNVERIFIED"
    else:
        assessment = "UNVERIFIED_RESPONSE"

    return {
        "assessment": assessment,
        "cf7_detected": cf7_detected,
        "url_fragment": fragment or None,
        "success_matches": success_matches,
        "failure_matches": failure_matches,
        "cf7_response": cf7_response,
    }


def diagnose_submit(plan: dict) -> dict:
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
        return {"decision": "BLOCKED_NO_PAYLOAD", "store_id": store_id}

    action_host = urlparse(action).netloc.lower()
    form_host = urlparse(form_url).netloc.lower()
    if not action_host or not form_host or not (
        action_host == form_host or action_host.endswith("." + form_host) or form_host.endswith("." + action_host)
    ):
        return {"decision": "BLOCKED_ACTION_HOST", "store_id": store_id}

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
        "diagnostic": True,
    }

    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            raw = res.read(500_000)
            charset = res.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            compact = re.sub(r"\s+", " ", text).strip()
            final_url = res.geturl()
            analysis = _analyze_response(text, final_url)
            result = {
                "decision": "DIAGNOSTIC_RESPONSE",
                "store_id": store_id,
                "request_sent": True,
                "http_status": getattr(res, "status", None),
                "final_url": final_url,
                "content_type": res.headers.get("Content-Type"),
                **analysis,
                "response_excerpt": compact[:1200],
            }
            log_item.update({k: v for k, v in result.items() if k != "response_excerpt"})
            append_submission_log(log_item)
            return result
    except HTTPError as exc:
        try:
            body = exc.read(200_000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        result = {
            "decision": "DIAGNOSTIC_HTTP_ERROR",
            "store_id": store_id,
            "request_sent": True,
            "http_status": exc.code,
            "response_excerpt": re.sub(r"\s+", " ", body).strip()[:1200],
        }
        log_item.update({k: v for k, v in result.items() if k != "response_excerpt"})
        append_submission_log(log_item)
        return result
    except (URLError, TimeoutError, ValueError) as exc:
        result = {
            "decision": "DIAGNOSTIC_TRANSPORT_FAILED",
            "store_id": store_id,
            "request_sent": False,
            "reason": type(exc).__name__,
        }
        log_item.update(result)
        append_submission_log(log_item)
        return result


def main() -> None:
    p = argparse.ArgumentParser(description="Submit one explicitly confirmed form and print response diagnostics.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--store-id", required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument("--confirm-store-id")
    args = p.parse_args()

    if not args.live:
        raise SystemExit("Diagnostic submission requires --live")
    if args.confirm_store_id != args.store_id:
        raise SystemExit("--confirm-store-id must equal --store-id")

    conn = sqlite3.connect(args.db)
    try:
        row = conn.execute(
            """
            SELECT store_id, store_name, lp_url, contact_form_url
            FROM leads
            WHERE campaign_id=?
              AND store_id=?
              AND contact_status='READY_FORM'
              AND screening_status='FORM_AUTO_READY'
              AND send_allowed=1
              AND sales_status='READY'
              AND human_action=0
            """,
            (CAMPAIGN_ID, args.store_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise SystemExit("No eligible row found")

    plan = build_plan(*row)
    print("plan=" + json.dumps({k: v for k, v in plan.items() if k != "_payload"}, ensure_ascii=False))
    if plan.get("decision") != "SEND_READY":
        raise SystemExit("Store is not SEND_READY")

    result = diagnose_submit(plan)
    print("diagnostic=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

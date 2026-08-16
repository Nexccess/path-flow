from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from form_preflight import CAMPAIGN_ID, USER_AGENT
from form_sender import build_plan, previous_submission_attempt

TIMEOUT_SECONDS = 15


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

    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            raw = res.read(500_000)
            charset = res.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            compact = re.sub(r"\s+", " ", text).strip()
            return {
                "decision": "DIAGNOSTIC_RESPONSE",
                "store_id": store_id,
                "request_sent": True,
                "http_status": getattr(res, "status", None),
                "final_url": res.geturl(),
                "content_type": res.headers.get("Content-Type"),
                "response_excerpt": compact[:1200],
            }
    except HTTPError as exc:
        try:
            body = exc.read(200_000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {
            "decision": "DIAGNOSTIC_HTTP_ERROR",
            "store_id": store_id,
            "request_sent": True,
            "http_status": exc.code,
            "response_excerpt": re.sub(r"\s+", " ", body).strip()[:1200],
        }
    except (URLError, TimeoutError, ValueError) as exc:
        return {
            "decision": "DIAGNOSTIC_TRANSPORT_FAILED",
            "store_id": store_id,
            "request_sent": False,
            "reason": type(exc).__name__,
        }


def main() -> None:
    p = argparse.ArgumentParser(description="Submit one explicitly confirmed form and print a response diagnostic excerpt.")
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

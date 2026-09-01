from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from engine import close_no_response, due_actions, mark_sent
from graph_mail import GraphConfig, GraphMailClient
from templates import render

DEFAULT_CAMPAIGN_ID = "PF-NAIL-001"
CAMPAIGN_ID = DEFAULT_CAMPAIGN_ID  # backward compatibility
JST = timezone(timedelta(hours=9))
SEND_START = time(10, 0)
SEND_END = time(19, 0)

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
BAD_EMAIL_PARTS = (
    "example@",
    "@example.",
    "sentry",
    "wixpress.com",
    "noreply",
    "no-reply",
    "donotreply",
)
BAD_LOCAL_ENDINGS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
)

SEND_RESERVATIONS = {
    "initial": ("READY", "SENDING_INITIAL"),
    "followup1": ("SENT", "SENDING_FOLLOWUP_1"),
    "followup2": ("FOLLOWUP_1", "SENDING_FOLLOWUP_2"),
}


def is_safe_email(value: str | None) -> bool:
    if not value:
        return False
    email = value.strip()
    low = email.lower()
    if not EMAIL_RE.fullmatch(email):
        return False
    if any(part in low for part in BAD_EMAIL_PARTS):
        return False
    local = low.split("@", 1)[0]
    if local.endswith(BAD_LOCAL_ENDINGS):
        return False
    return True


def within_send_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(JST)
    if now.weekday() >= 5:
        return False
    current = now.timetz().replace(tzinfo=None)
    return SEND_START <= current <= SEND_END


def campaign_is_sales_ready(conn: sqlite3.Connection, campaign_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT status FROM campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] == "SALES_READY")


def reserve_send(
    conn: sqlite3.Connection,
    store_id: str,
    stage: str,
    campaign_id: str,
) -> bool:
    expected_status, reserved_status = SEND_RESERVATIONS[stage]
    at = datetime.now(JST).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        UPDATE leads
        SET sales_status=?, updated_at=?
        WHERE campaign_id=? AND store_id=?
          AND sales_status=? AND human_action=0
        """,
        (reserved_status, at, campaign_id, store_id, expected_status),
    )
    conn.commit()
    return cur.rowcount == 1


def run(db: Path, live: bool = False, max_sends: int | None = None, campaign_id: str = DEFAULT_CAMPAIGN_ID) -> dict:
    conn = sqlite3.connect(db)
    counts = {
        "initial": 0,
        "followup1": 0,
        "followup2": 0,
        "close": 0,
        "skipped": 0,
        "blocked_invalid_email": 0,
        "blocked_deploy_not_ready": 0,
        "blocked_send_window": 0,
        "blocked_campaign_not_sales_ready": 0,
        "blocked_live_without_limit": 0,
        "blocked_duplicate_or_inflight": 0,
        "send_error_requires_reconcile": 0,
    }

    if live and max_sends is None:
        counts["blocked_live_without_limit"] = 1
        print("LIVE SEND BLOCKED: --max-sends is required for every live run. No messages were sent.")
        conn.close()
        return counts

    if live and not campaign_is_sales_ready(conn, campaign_id):
        counts["blocked_campaign_not_sales_ready"] = 1
        print(
            f"LIVE SEND BLOCKED: campaign {campaign_id} is not SALES_READY. "
            "No messages were sent."
        )
        conn.close()
        return counts

    if live and not within_send_window():
        counts["blocked_send_window"] = 1
        print(
            "LIVE SEND BLOCKED: allowed window is weekdays 10:00-19:00 JST. "
            "No messages were sent."
        )
        conn.close()
        return counts

    client = GraphMailClient(GraphConfig.from_env()) if live else None
    try:
        actions = due_actions(conn, campaign_id=campaign_id)
        sent_count = 0
        for store_id, store_name, stage in actions:
            if stage == "close":
                if live:
                    close_no_response(conn, store_id, campaign_id=campaign_id)
                    conn.commit()
                else:
                    print(f"DRY-RUN\tclose\t{store_id}\t{store_name}")
                counts["close"] += 1
                continue

            if max_sends is not None and sent_count >= max_sends:
                break

            lead_cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
            if "deploy_status" not in lead_cols:
                counts["blocked_deploy_not_ready"] += 1
                print(f"BLOCKED\tdeploy-status-missing\t{store_id}\t{store_name}")
                continue

            row = conn.execute(
                """
                SELECT lp_url, deploy_status, email, contact_status, send_allowed, human_action, sales_status
                FROM leads WHERE campaign_id=? AND store_id=?
                """,
                (campaign_id, store_id),
            ).fetchone()
            if not row:
                counts["skipped"] += 1
                continue
            lp_url, deploy_status, email, contact_status, send_allowed, human_action, sales_status = row
            if deploy_status != "READY" or not lp_url:
                counts["blocked_deploy_not_ready"] += 1
                print(
                    f"BLOCKED\tdeploy-not-ready\t{store_id}\t{store_name}\t"
                    f"deploy_status={deploy_status or '-'}\tlp_url={'set' if lp_url else 'missing'}"
                )
                continue
            if human_action or not email or contact_status != "READY_EMAIL" or not send_allowed:
                counts["skipped"] += 1
                continue

            if not is_safe_email(email):
                counts["blocked_invalid_email"] += 1
                print(f"BLOCKED\tinvalid-email\t{store_id}\t{store_name}\t{email}")
                continue

            subject, body = render(stage, store_name, store_id, lp_url)
            if live:
                assert client is not None
                if not reserve_send(conn, store_id, stage, campaign_id):
                    counts["blocked_duplicate_or_inflight"] += 1
                    print(f"BLOCKED\tduplicate-or-inflight\t{stage}\t{store_id}\t{store_name}")
                    continue

                try:
                    client.send_text(email, subject, body, dry_run=False)
                    mark_sent(conn, store_id, stage, campaign_id=campaign_id)
                    conn.commit()
                except Exception:
                    counts["send_error_requires_reconcile"] += 1
                    print(f"RECONCILE_REQUIRED\t{stage}\t{store_id}\t{store_name}\t{email}")
                    raise
            else:
                print(f"DRY-RUN\t{stage}\t{store_id}\t{store_name}\t{email}\t{subject}")

            counts[stage] += 1
            sent_count += 1
        return counts
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    p.add_argument("--live", action="store_true", help="Actually send mail. Omit for dry-run.")
    p.add_argument("--max-sends", type=int)
    args = p.parse_args()
    result = run(args.db, live=args.live, max_sends=args.max_sends, campaign_id=args.campaign_id)
    print(result)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from engine import close_no_response, due_actions, mark_sent
from graph_mail import GraphConfig, GraphMailClient
from templates import render

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))
SEND_START = time(10, 30)
SEND_END = time(16, 30)

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
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    current = now.timetz().replace(tzinfo=None)
    return SEND_START <= current <= SEND_END


def run(db: Path, live: bool = False, max_sends: int | None = None) -> dict:
    conn = sqlite3.connect(db)
    counts = {
        "initial": 0,
        "followup1": 0,
        "followup2": 0,
        "close": 0,
        "skipped": 0,
        "blocked_invalid_email": 0,
        "blocked_send_window": 0,
    }

    if live and not within_send_window():
        counts["blocked_send_window"] = 1
        print(
            "LIVE SEND BLOCKED: allowed window is weekdays 10:30-16:30 JST. "
            "No messages were sent."
        )
        conn.close()
        return counts

    client = GraphMailClient(GraphConfig.from_env()) if live else None
    try:
        actions = due_actions(conn)
        sent_count = 0
        for store_id, store_name, stage in actions:
            if stage == "close":
                if live:
                    close_no_response(conn, store_id)
                    conn.commit()
                else:
                    print(f"DRY-RUN\tclose\t{store_id}\t{store_name}")
                counts["close"] += 1
                continue

            if max_sends is not None and sent_count >= max_sends:
                break

            row = conn.execute(
                """
                SELECT lp_url, email, contact_status, send_allowed, human_action, sales_status
                FROM leads WHERE campaign_id=? AND store_id=?
                """,
                (CAMPAIGN_ID, store_id),
            ).fetchone()
            if not row:
                counts["skipped"] += 1
                continue
            lp_url, email, contact_status, send_allowed, human_action, sales_status = row
            if human_action or not email or contact_status != "READY_EMAIL" or not send_allowed:
                counts["skipped"] += 1
                continue

            # Defense in depth: even READY_EMAIL rows are revalidated immediately before send.
            if not is_safe_email(email):
                counts["blocked_invalid_email"] += 1
                print(f"BLOCKED\tinvalid-email\t{store_id}\t{store_name}\t{email}")
                continue

            subject, body = render(stage, store_name, store_id, lp_url)
            if live:
                assert client is not None
                client.send_text(email, subject, body, dry_run=False)
                mark_sent(conn, store_id, stage)
                conn.commit()
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
    p.add_argument("--live", action="store_true", help="Actually send mail. Omit for dry-run.")
    p.add_argument("--max-sends", type=int)
    args = p.parse_args()
    result = run(args.db, live=args.live, max_sends=args.max_sends)
    print(result)


if __name__ == "__main__":
    main()

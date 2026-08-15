from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))


def now() -> datetime:
    return datetime.now(JST)


def report(conn: sqlite3.Connection) -> str:
    total = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=?", (CAMPAIGN_ID,)
    ).fetchone()[0]
    human = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND human_action=1", (CAMPAIGN_ID,)
    ).fetchone()[0]
    sent = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND initial_sent_at IS NOT NULL", (CAMPAIGN_ID,)
    ).fetchone()[0]
    closed = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND sales_status='CLOSED_NO_RESPONSE'", (CAMPAIGN_ID,)
    ).fetchone()[0]
    responded = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND response_at IS NOT NULL", (CAMPAIGN_ID,)
    ).fetchone()[0]
    ready_email = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND contact_status='READY_EMAIL'", (CAMPAIGN_ID,)
    ).fetchone()[0]
    ready_form = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND contact_status='READY_FORM'", (CAMPAIGN_ID,)
    ).fetchone()[0]

    cutoff = (now() - timedelta(hours=24)).isoformat(timespec="seconds")
    overdue_rows = conn.execute(
        """
        SELECT store_id, store_name, response_type, response_at
        FROM leads
        WHERE campaign_id=? AND human_action=1 AND response_at IS NOT NULL AND response_at <= ?
        ORDER BY response_at
        """,
        (CAMPAIGN_ID, cutoff),
    ).fetchall()

    today = now().date().isoformat()
    new_responses = conn.execute(
        """
        SELECT COUNT(*) FROM leads
        WHERE campaign_id=? AND response_at IS NOT NULL AND substr(response_at,1,10)=?
        """,
        (CAMPAIGN_ID, today),
    ).fetchone()[0]

    lines = [
        "Path-Flow Campaign #001 Daily Report",
        f"対象: {total}店舗",
        f"初回送信済: {sent}",
        f"新規反響(本日): {new_responses}",
        f"累計反響: {responded}",
        f"要対応: {human}",
        f"24時間超未対応: {len(overdue_rows)}",
        f"Close済: {closed}",
        f"送信可能Email: {ready_email}",
        f"Form候補: {ready_form}",
    ]
    if overdue_rows:
        lines.append("")
        lines.append("【24時間超未対応】")
        for store_id, store_name, response_type, response_at in overdue_rows:
            lines.append(f"- {store_name} / {response_type or 'UNKNOWN'} / {response_at} / PF:{store_id}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    args = p.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        print(report(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

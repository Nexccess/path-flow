from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"
JST = timezone(timedelta(hours=9))
ACTIONABLE_CONTACT_STATUSES = {
    "READY_EMAIL",
    "READY_FORM",
    "READY_LINE",
    "READY_INSTAGRAM",
    "READY_SMS",
}


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def parse_dt(value: str | None):
    return datetime.fromisoformat(value) if value else None


def add_event(conn, store_id: str, event_type: str, payload=None, external_message_id=None):
    conn.execute(
        """
        INSERT OR IGNORE INTO events
          (campaign_id, store_id, event_type, event_at, external_message_id, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            CAMPAIGN_ID,
            store_id,
            event_type,
            now_iso(),
            external_message_id,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )


def mark_sent(conn, store_id: str, stage: str, at: str | None = None):
    at = at or now_iso()
    mapping = {
        "initial": ("SENT", "initial_sent_at", "DM_INITIAL_SENT"),
        "followup1": ("FOLLOWUP_1", "followup1_sent_at", "DM_FOLLOWUP1_SENT"),
        "followup2": ("FOLLOWUP_2", "followup2_sent_at", "DM_FOLLOWUP2_SENT"),
    }
    status, field, event_type = mapping[stage]
    lead = conn.execute(
        "SELECT sales_status, human_action FROM leads WHERE campaign_id=? AND store_id=?",
        (CAMPAIGN_ID, store_id),
    ).fetchone()
    if not lead:
        raise ValueError(f"Unknown store_id: {store_id}")
    if lead[1] or lead[0] in {"RESPONDED", "HUMAN_ACTION", "WON_ONE_TIME", "WON_MAINTENANCE", "LOST"}:
        raise ValueError(f"Automated send blocked for store_id={store_id}, status={lead[0]}")
    conn.execute(
        f"UPDATE leads SET sales_status=?, {field}=?, updated_at=? WHERE campaign_id=? AND store_id=?",
        (status, at, at, CAMPAIGN_ID, store_id),
    )
    add_event(conn, store_id, event_type, {"sent_at": at})


def mark_response(conn, store_id: str, response_type: str = "UNKNOWN", external_message_id=None):
    at = now_iso()
    conn.execute(
        """
        UPDATE leads
        SET sales_status='HUMAN_ACTION', response_type=?, human_action=1,
            response_at=COALESCE(response_at, ?), updated_at=?
        WHERE campaign_id=? AND store_id=?
        """,
        (response_type, at, at, CAMPAIGN_ID, store_id),
    )
    add_event(
        conn,
        store_id,
        "RESPONSE_RECEIVED",
        {"response_type": response_type},
        external_message_id=external_message_id,
    )


def due_actions(conn, now: datetime | None = None):
    now = now or datetime.now(JST)
    rows = conn.execute(
        """
        SELECT store_id, store_name, sales_status, initial_sent_at,
               followup1_sent_at, followup2_sent_at, human_action, contact_status
        FROM leads
        WHERE campaign_id=?
        """,
        (CAMPAIGN_ID,),
    ).fetchall()
    actions = []
    for store_id, store_name, status, initial_at, f1_at, f2_at, human_action, contact_status in rows:
        if human_action:
            continue
        if status == "READY":
            if contact_status in ACTIONABLE_CONTACT_STATUSES:
                actions.append((store_id, store_name, "initial"))
            continue
        initial = parse_dt(initial_at)
        follow1 = parse_dt(f1_at)
        follow2 = parse_dt(f2_at)
        if status == "SENT" and initial and now >= initial + timedelta(days=4):
            actions.append((store_id, store_name, "followup1"))
        elif status == "FOLLOWUP_1" and follow1 and now >= follow1 + timedelta(days=5):
            actions.append((store_id, store_name, "followup2"))
        elif status == "FOLLOWUP_2" and follow2 and now >= follow2 + timedelta(days=5):
            actions.append((store_id, store_name, "close"))
    return actions


def close_no_response(conn, store_id: str):
    at = now_iso()
    conn.execute(
        """
        UPDATE leads
        SET sales_status='CLOSED_NO_RESPONSE', closed_at=?, close_reason='NO_RESPONSE', updated_at=?
        WHERE campaign_id=? AND store_id=? AND human_action=0
        """,
        (at, at, CAMPAIGN_ID, store_id),
    )
    add_event(conn, store_id, "CLOSED_NO_RESPONSE")


def daily_summary(conn):
    result = {}
    for status, count in conn.execute(
        "SELECT sales_status, COUNT(*) FROM leads WHERE campaign_id=? GROUP BY sales_status",
        (CAMPAIGN_ID,),
    ):
        result[status] = count
    result["HUMAN_ACTION"] = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND human_action=1",
        (CAMPAIGN_ID,),
    ).fetchone()[0]
    result["ACTIONABLE_CONTACTS"] = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND contact_status IN (?,?,?,?,?)",
        (CAMPAIGN_ID, *sorted(ACTIONABLE_CONTACT_STATUSES)),
    ).fetchone()[0]
    result["TOTAL"] = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE campaign_id=?",
        (CAMPAIGN_ID,),
    ).fetchone()[0]
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--summary", action="store_true")
    p.add_argument("--due", action="store_true")
    args = p.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        if args.summary:
            print(json.dumps(daily_summary(conn), ensure_ascii=False, indent=2))
        if args.due:
            for action in due_actions(conn):
                print("\t".join(action))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

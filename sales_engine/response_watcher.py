from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import add_event, mark_response
from graph_mail import GraphConfig, GraphMailClient, extract_store_id, sender_address
from ollama_client import OllamaClient

DEFAULT_CAMPAIGN_ID = "PF-NAIL-001"
CAMPAIGN_ID = DEFAULT_CAMPAIGN_ID  # backward compatibility
JST = timezone(timedelta(hours=9))


def find_store_by_sender(conn: sqlite3.Connection, sender: str | None, campaign_id: str = DEFAULT_CAMPAIGN_ID) -> str | None:
    if not sender:
        return None
    row = conn.execute(
        """
        SELECT store_id FROM leads
        WHERE campaign_id=? AND lower(email)=lower(?)
        ORDER BY updated_at DESC LIMIT 1
        """,
        (campaign_id, sender),
    ).fetchone()
    return row[0] if row else None


def store_name_for(conn: sqlite3.Connection, store_id: str, campaign_id: str = DEFAULT_CAMPAIGN_ID) -> str:
    row = conn.execute(
        "SELECT store_name FROM leads WHERE campaign_id=? AND store_id=?",
        (campaign_id, store_id),
    ).fetchone()
    return row[0] if row else store_id


def apply_classification(conn: sqlite3.Connection, store_id: str, result: dict, message_id: str, campaign_id: str = DEFAULT_CAMPAIGN_ID) -> None:
    label = result.get("label", "UNKNOWN")
    confidence = float(result.get("confidence", 0))
    needs_human = bool(result.get("needs_human", True))

    if label in {"DECLINED", "AUTO_REPLY"} and confidence >= 0.70:
        at = datetime.now(JST).isoformat(timespec="seconds")
        sales_status = "LOST" if label == "DECLINED" else "CLOSED_NO_RESPONSE"
        close_reason = "DECLINED" if label == "DECLINED" else "AUTO_REPLY"
        conn.execute(
            """
            UPDATE leads
            SET sales_status=?, response_type=?, human_action=0,
                response_at=COALESCE(response_at, ?), closed_at=?, close_reason=?, updated_at=?
            WHERE campaign_id=? AND store_id=?
            """,
            (sales_status, label, at, at, close_reason, at, campaign_id, store_id),
        )
        add_event(
            conn,
            store_id,
            "RESPONSE_CLASSIFIED",
            {"classification": result, "auto_closed": True},
            external_message_id=message_id,
            campaign_id=campaign_id,
        )
        return

    # Safety-first: anything uncertain or commercially relevant goes to a human.
    mark_response(conn, store_id, response_type=label, external_message_id=message_id, campaign_id=campaign_id)
    add_event(
        conn,
        store_id,
        "RESPONSE_CLASSIFIED",
        {"classification": result, "auto_closed": False, "needs_human": needs_human},
        external_message_id=message_id,
        campaign_id=campaign_id,
    )


def run(db: Path, lookback_hours: int = 72, use_ollama: bool = True, campaign_id: str = DEFAULT_CAMPAIGN_ID) -> tuple[int, int, int]:
    client = GraphMailClient(GraphConfig.from_env())
    ai = OllamaClient() if use_ollama else None
    since = datetime.now(JST) - timedelta(hours=lookback_hours)
    messages = client.recent_inbox(since, top=200)
    conn = sqlite3.connect(db)
    matched = 0
    unmatched = 0
    classified = 0
    try:
        for msg in messages:
            message_id = msg.get("id") or msg.get("internetMessageId")
            if not message_id:
                continue
            already = conn.execute(
                "SELECT 1 FROM events WHERE event_type='RESPONSE_RECEIVED' AND external_message_id=? LIMIT 1",
                (message_id,),
            ).fetchone()
            if already:
                continue

            store_id = extract_store_id(msg.get("subject"))
            if not store_id:
                store_id = find_store_by_sender(conn, sender_address(msg), campaign_id=campaign_id)
            if not store_id:
                unmatched += 1
                continue

            if ai is None:
                mark_response(conn, store_id, response_type="UNKNOWN", external_message_id=message_id, campaign_id=campaign_id)
                conn.commit()
                matched += 1
                continue

            try:
                result = ai.classify_response(
                    store_name=store_name_for(conn, store_id, campaign_id=campaign_id),
                    subject=msg.get("subject") or "",
                    body_preview=msg.get("bodyPreview") or "",
                )
            except Exception as exc:
                # Ollama failure must never lose a reply. Escalate to human instead.
                result = {
                    "label": "UNKNOWN",
                    "confidence": 0.0,
                    "needs_human": True,
                    "reason": f"ollama_error:{type(exc).__name__}",
                }

            apply_classification(conn, store_id, result, message_id, campaign_id=campaign_id)
            conn.commit()
            matched += 1
            classified += 1
            print(
                f"reply\t{store_id}\t{result.get('label')}\tconfidence={result.get('confidence')}\t"
                f"human={result.get('needs_human')}"
            )
        return matched, unmatched, classified
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    p.add_argument("--lookback-hours", type=int, default=72)
    p.add_argument("--no-ollama", action="store_true", help="Disable Ollama classification and send all matched replies to HUMAN_ACTION")
    p.add_argument("--ollama-health", action="store_true", help="Only test the configured Ollama endpoint/model")
    args = p.parse_args()

    if args.ollama_health:
        result = OllamaClient().health()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    matched, unmatched, classified = run(args.db, args.lookback_hours, use_ollama=not args.no_ollama, campaign_id=args.campaign_id)
    print(f"matched_responses={matched} unmatched_messages={unmatched} classified={classified}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

CAMPAIGN_ID = "PF-NAIL-001"


def truthy(value) -> bool:
    return bool(value)


def load_auto_sendable(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT store_id, store_name, contact_status, screening_status, send_allowed,
               email, contact_form_url, line_url, instagram_url,
               sales_status, lp_url, google_rating, google_review_count
        FROM leads
        WHERE campaign_id=?
          AND send_allowed=1
          AND (
                (contact_status='READY_EMAIL' AND email IS NOT NULL AND TRIM(email) <> '')
             OR (contact_status='READY_FORM' AND screening_status='FORM_AUTO_READY'
                 AND contact_form_url IS NOT NULL AND TRIM(contact_form_url) <> '')
          )
        ORDER BY store_id
        """,
        (CAMPAIGN_ID,),
    ).fetchall()
    return rows


def describe(row: sqlite3.Row) -> dict:
    return {
        "store_id": row["store_id"],
        "store_name": row["store_name"],
        "contact_status": row["contact_status"],
        "screening_status": row["screening_status"],
        "sales_status": row["sales_status"],
        "google_rating": row["google_rating"],
        "google_review_count": row["google_review_count"],
        "channels": {
            "email": truthy(row["email"]),
            "form": truthy(row["contact_form_url"]),
            "line": truthy(row["line_url"]),
            "instagram": truthy(row["instagram_url"]),
        },
    }


def count_rule(rows, predicate):
    matched = [describe(r) for r in rows if predicate(r)]
    return {"count": len(matched), "store_ids": [x["store_id"] for x in matched]}


def main() -> None:
    p = argparse.ArgumentParser(description="Infer plausible HIGH/MEDIUM split rules inside auto-sendable PF-NAIL-001 leads.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db)
    try:
        rows = load_auto_sendable(conn)

        rules = {
            "email_route": lambda r: r["contact_status"] == "READY_EMAIL",
            "form_auto_route": lambda r: r["contact_status"] == "READY_FORM" and r["screening_status"] == "FORM_AUTO_READY",
            "has_line": lambda r: truthy(r["line_url"]),
            "has_instagram": lambda r: truthy(r["instagram_url"]),
            "has_line_and_instagram": lambda r: truthy(r["line_url"]) and truthy(r["instagram_url"]),
            "has_email_line_instagram": lambda r: truthy(r["email"]) and truthy(r["line_url"]) and truthy(r["instagram_url"]),
            "already_sent": lambda r: r["sales_status"] == "SENT",
            "rating_ge_4_5": lambda r: r["google_rating"] is not None and float(r["google_rating"]) >= 4.5,
            "reviews_ge_50": lambda r: r["google_review_count"] is not None and int(r["google_review_count"]) >= 50,
            "reviews_ge_100": lambda r: r["google_review_count"] is not None and int(r["google_review_count"]) >= 100,
            "rating_ge_4_5_and_reviews_ge_50": lambda r: (
                r["google_rating"] is not None and float(r["google_rating"]) >= 4.5
                and r["google_review_count"] is not None and int(r["google_review_count"]) >= 50
            ),
            "rating_ge_4_5_and_reviews_ge_100": lambda r: (
                r["google_rating"] is not None and float(r["google_rating"]) >= 4.5
                and r["google_review_count"] is not None and int(r["google_review_count"]) >= 100
            ),
        }

        evaluated = {name: count_rule(rows, fn) for name, fn in rules.items()}
        exact_four = {name: result for name, result in evaluated.items() if result["count"] == 4}

        output = {
            "campaign_id": CAMPAIGN_ID,
            "auto_sendable_count": len(rows),
            "auto_sendable_rows": [describe(r) for r in rows],
            "candidate_rules": evaluated,
            "rules_matching_high_count_4": exact_four,
            "note": "This tool only identifies simple observable predicates that produce four rows; it does not assert which rule was historically intended.",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

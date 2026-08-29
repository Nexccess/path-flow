from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from import_lead_value_signals import run as import_signals

CAMPAIGN_ID = "PF-NAIL-001"


def truthy(value) -> bool:
    return bool(value)


def has_column(conn: sqlite3.Connection, column: str) -> bool:
    return any(row[1] == column for row in conn.execute("PRAGMA table_info(leads)").fetchall())


def load_auto_sendable(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    rating_expr = "google_rating" if has_column(conn, "google_rating") else "NULL AS google_rating"
    reviews_expr = "google_review_count" if has_column(conn, "google_review_count") else "NULL AS google_review_count"
    rows = conn.execute(
        f"""
        SELECT store_id, store_name, contact_status, screening_status, send_allowed,
               email, contact_form_url, line_url, instagram_url,
               sales_status, lp_url, {rating_expr}, {reviews_expr}
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


def evaluate(db: Path, csv_path: Path | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="priority_infer_") as tmp:
        work_db = Path(tmp) / db.name
        shutil.copy2(db, work_db)

        signals = None
        if csv_path is not None:
            signals = import_signals(
                csv_path=csv_path,
                db=work_db,
                campaign_id=CAMPAIGN_ID,
                apply=True,
            )

        conn = sqlite3.connect(work_db)
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

            return {
                "campaign_id": CAMPAIGN_ID,
                "source_db_unchanged": True,
                "signals": signals,
                "auto_sendable_count": len(rows),
                "auto_sendable_rows": [describe(r) for r in rows],
                "candidate_rules": evaluated,
                "rules_matching_high_count_4": exact_four,
                "note": "Simple observable predicates only. Matching four rows does not prove historical intent.",
            }
        finally:
            conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Infer plausible HIGH/MEDIUM split rules inside auto-sendable PF-NAIL-001 leads without modifying the source DB.")
    p.add_argument("--db", type=Path, default=Path("sales_engine.db"))
    p.add_argument("--csv", type=Path, help="Optional 113-store CSV. When supplied, Google rating/review signals are imported into a temporary DB copy.")
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")
    if args.csv is not None and not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    print(json.dumps(evaluate(args.db, args.csv), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_SOURCE_DB = REPO_ROOT / "lead_intelligence" / "data" / "working" / "lead_intelligence.db"
DEFAULT_TARGET_DB = REPO_ROOT / "sales_engine.db"
SCHEMA = HERE / "schema.sql"


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def source_rows(conn: sqlite3.Connection, lead_ids: list[int] | None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    lead_cols = table_columns(conn, "leads")

    place_expr = "NULL"
    if "place_id" in lead_cols:
        place_expr = "l.place_id"
    elif "google_place_id" in lead_cols:
        place_expr = "l.google_place_id"

    category_expr = "l.category" if "category" in lead_cols else "NULL"
    area_expr = "l.area" if "area" in lead_cols else "NULL"
    website_expr = "l.website_url" if "website_url" in lead_cols else "q.website_url"

    sql = f"""
        SELECT
          q.lead_id,
          q.company_name,
          q.lp_url,
          q.deploy_status,
          q.status AS queue_status,
          {place_expr} AS google_place_id,
          {category_expr} AS industry,
          {area_expr} AS area,
          {website_expr} AS store_url
        FROM sales_queue q
        JOIN leads l ON l.lead_id=q.lead_id
        WHERE q.deploy_status='READY'
          AND q.lp_url IS NOT NULL
          AND trim(q.lp_url)!=''
    """
    params: list[object] = []
    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        sql += f" AND q.lead_id IN ({placeholders})"
        params.extend(lead_ids)
    sql += " ORDER BY q.lead_id"
    return conn.execute(sql, params).fetchall()


def ensure_target_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))


def sync(
    source_db: Path,
    target_db: Path,
    target_campaign_id: str,
    lead_ids: list[int] | None = None,
    apply: bool = False,
) -> dict:
    if not source_db.exists():
        raise SystemExit(f"source DB not found: {source_db}")

    source = sqlite3.connect(source_db)
    target = sqlite3.connect(target_db)
    try:
        rows = source_rows(source, lead_ids)
        summary = {
            "campaign_id": target_campaign_id,
            "eligible": len(rows),
            "applied": 0,
            "dry_run": not apply,
        }

        if not rows:
            return summary

        ensure_target_schema(target)

        if apply:
            target.execute(
                """
                INSERT OR IGNORE INTO campaigns
                  (campaign_id, name, industry, status)
                VALUES (?, ?, ?, 'DRAFT')
                """,
                (
                    target_campaign_id,
                    target_campaign_id,
                    rows[0]["industry"] if len({clean(r["industry"]) for r in rows if clean(r["industry"])}) == 1 else None,
                ),
            )

        for row in rows:
            store_id = str(row["lead_id"])
            print(
                f"{'APPLY' if apply else 'DRY-RUN'}\t{target_campaign_id}\t{store_id}\t"
                f"{row['company_name']}\t{row['deploy_status']}\t{row['lp_url']}"
            )
            if not apply:
                continue

            target.execute(
                """
                INSERT INTO leads (
                  campaign_id, store_id, google_place_id, store_name, area, industry,
                  store_url, lp_url, deploy_status, screening_status, sales_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', 'READY')
                ON CONFLICT(campaign_id, store_id) DO UPDATE SET
                  google_place_id=COALESCE(excluded.google_place_id, leads.google_place_id),
                  store_name=excluded.store_name,
                  area=COALESCE(excluded.area, leads.area),
                  industry=COALESCE(excluded.industry, leads.industry),
                  store_url=COALESCE(excluded.store_url, leads.store_url),
                  lp_url=excluded.lp_url,
                  deploy_status=excluded.deploy_status,
                  screening_status='READY',
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    target_campaign_id,
                    store_id,
                    clean(row["google_place_id"]),
                    clean(row["company_name"]) or store_id,
                    clean(row["area"]),
                    clean(row["industry"]),
                    clean(row["store_url"]),
                    clean(row["lp_url"]),
                    clean(row["deploy_status"]),
                ),
            )
            summary["applied"] += 1

        if apply:
            target.commit()
        else:
            target.rollback()
        return summary
    finally:
        source.close()
        target.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Bridge deployed LP leads into the Sales Engine without enabling sends.")
    p.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    p.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    p.add_argument("--target-campaign-id", required=True)
    p.add_argument("--lead-id", type=int, action="append", dest="lead_ids")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    result = sync(
        source_db=args.source_db,
        target_db=args.target_db,
        target_campaign_id=args.target_campaign_id,
        lead_ids=args.lead_ids,
        apply=args.apply,
    )
    print(result)


if __name__ == "__main__":
    main()

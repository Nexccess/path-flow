from __future__ import annotations

import argparse
import sqlite3
import sys

import screening_automation as base

_SOURCE: str | None = None
_original_load_rows = base.load_rows
_original_classify = base.classify
_original_persist_decision = base.persist_decision

NON_TARGET_HOST_HINTS = (
    "beauty.rakuten.co.jp",
    "beauty-park.jp",
    "nailie.jp",
    "machi-biz.com",
    "beautifyjp.net",
    "minimodel.jp",
    "nailbook.jp",
)


def _host(url: str | None) -> str:
    return base.host_of(url)


def load_rows_by_source(conn: sqlite3.Connection, limit: int | None):
    if not _SOURCE:
        return _original_load_rows(conn, limit)

    cols = base.table_columns(conn, "leads")
    required = {
        "lead_id", "company_name", "category", "area", "website_url",
        "legacy_email", "legacy_form_url", "source",
    }
    missing = required - cols
    if missing:
        raise SystemExit(f"leads table missing required columns: {sorted(missing)}")

    sql = """
        SELECT l.lead_id, l.company_name, l.category, l.area, l.website_url,
               l.legacy_email, l.legacy_form_url,
               i.confidence AS discovery_confidence
        FROM leads l
        LEFT JOIN lead_discovery_intelligence i ON i.lead_id = l.lead_id
        WHERE l.source = ?
        ORDER BY l.lead_id
    """
    params: list[object] = [_SOURCE]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def classify_discovery(row: sqlite3.Row, target_categories: set[str], target_areas: set[str]):
    if _SOURCE == "lead-discovery-v1":
        website = row["website_url"]
        host = _host(website)
        if any(host == h or host.endswith("." + h) for h in NON_TARGET_HOST_HINTS):
            return base.Decision(
                "CLOSE",
                "NON_TARGET_PLATFORM",
                detail=website,
            )

        try:
            confidence = row["discovery_confidence"]
        except (IndexError, KeyError):
            confidence = None
        if confidence is None or float(confidence) < 0.5:
            return base.Decision(
                "HOLD",
                "LOW_CONFIDENCE_IDENTITY",
                detail=f"confidence={confidence if confidence is not None else '-'}",
            )
    return _original_classify(row, target_categories, target_areas)


def persist_discovery_decision(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    decision: base.Decision,
    campaign_id: str,
) -> None:
    _original_persist_decision(conn, row, decision, campaign_id)
    if decision.status != "GO":
        conn.execute(
            "DELETE FROM sales_queue WHERE campaign_id=? AND lead_id=?",
            (campaign_id, int(row["lead_id"])),
        )


def main() -> None:
    global _SOURCE

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source")
    known, remaining = parser.parse_known_args()
    _SOURCE = known.source

    base.load_rows = load_rows_by_source
    base.classify = classify_discovery
    base.persist_decision = persist_discovery_decision
    sys.argv = [sys.argv[0], *remaining]
    base.main()


if __name__ == "__main__":
    main()

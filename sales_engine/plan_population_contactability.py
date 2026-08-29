from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

PORTAL_HOST_HINTS = (
    "beauty.hotpepper.jp",
    "hotpepper.jp",
    "maps.google.",
    "google.com/maps",
    "tabelog.com",
    "minimodel.jp",
    "epark.jp",
    "rakuten.co.jp",
    "facebook.com",
    "instagram.com",
)


def norm(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower().split(":")[0] or None
    except Exception:
        return None


def is_portal(url: str | None) -> bool:
    if not url:
        return False
    low = url.lower()
    return any(h in low for h in PORTAL_HOST_HINTS)


def pct(n: int, d: int) -> float:
    return round((n / d * 100.0), 2) if d else 0.0


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only contactability planning for lead_intelligence population.")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--pilot-per-category", type=int, default=3)
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT lead_id, company_name, category, area, website_url,
                   legacy_email, legacy_form_url, rating, user_ratings_total
            FROM leads
            ORDER BY category, area, lead_id
            """
        ).fetchall()

        total = len(rows)
        overall = Counter()
        by_category: dict[str, Counter] = defaultdict(Counter)
        form_hosts = Counter()
        pilot: dict[str, list[dict]] = defaultdict(list)

        for r in rows:
            category = norm(r["category"]) or "(blank)"
            website = norm(r["website_url"])
            email = norm(r["legacy_email"])
            form = norm(r["legacy_form_url"])
            portal_form = is_portal(form)
            nonportal_form = bool(form and not portal_form)

            flags = {
                "website": bool(website),
                "email": bool(email),
                "form": bool(form),
                "portal_form": portal_form,
                "nonportal_form": nonportal_form,
                "email_or_nonportal_form": bool(email or nonportal_form),
                "website_only": bool(website and not email and not form),
                "no_digital_route": bool(not website and not email and not form),
            }
            for key, value in flags.items():
                if value:
                    overall[key] += 1
                    by_category[category][key] += 1
            overall["total"] += 1
            by_category[category]["total"] += 1

            if form:
                form_hosts[host(form) or "(no_host)"] += 1

            # Deterministic pilot: prefer rows with a website because live enrichment needs a fetchable source.
            if len(pilot[category]) < args.pilot_per_category and website:
                pilot[category].append({
                    "lead_id": r["lead_id"],
                    "company_name": r["company_name"],
                    "area": r["area"],
                    "website_url": website,
                    "legacy_email": email,
                    "legacy_form_url": form,
                    "rating": r["rating"],
                    "user_ratings_total": r["user_ratings_total"],
                })

        category_rows = []
        for category, c in sorted(by_category.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
            d = c["total"]
            category_rows.append({
                "category": category,
                "total": d,
                "website": c["website"],
                "email": c["email"],
                "form": c["form"],
                "portal_form": c["portal_form"],
                "nonportal_form": c["nonportal_form"],
                "email_or_nonportal_form": c["email_or_nonportal_form"],
                "candidate_route_pct": pct(c["email_or_nonportal_form"], d),
                "website_only": c["website_only"],
                "no_digital_route": c["no_digital_route"],
            })

        output = {
            "db": str(args.db),
            "total_leads": total,
            "overall": {
                **dict(overall),
                "candidate_route_pct": pct(overall["email_or_nonportal_form"], total),
                "portal_form_pct": pct(overall["portal_form"], total),
            },
            "by_category": category_rows,
            "top_form_hosts": [
                {"host": h, "count": n}
                for h, n in form_hosts.most_common(25)
            ],
            "pilot_per_category": args.pilot_per_category,
            "pilot_total": sum(len(v) for v in pilot.values()),
            "pilot": dict(pilot),
            "notes": [
                "email_or_nonportal_form is only a candidate-route upper bound, not AUTO_SENDABLE.",
                "Portal/booking/CAPTCHA/policy/dynamic-form checks still require live validation.",
                "Pilot rows are deterministic and website-backed for the next live-enrichment test.",
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

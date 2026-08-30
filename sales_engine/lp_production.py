from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_DB = REPO_ROOT / "lead_intelligence" / "data" / "working" / "lead_intelligence.db"
DEFAULT_TEMPLATE = REPO_ROOT / "index.html"
DEFAULT_OUTPUT = REPO_ROOT / "generated"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"
VERSION = "lp-production-e2e-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def ensure_queue_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sales_queue)")}
    additions = {
        "lp_status": "TEXT",
        "lp_path": "TEXT",
        "lp_url": "TEXT",
        "deploy_status": "TEXT",
        "generated_at": "TEXT",
        "deployed_at": "TEXT",
        "lp_version": "TEXT",
        "last_error": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE sales_queue ADD COLUMN {name} {sql_type}")
    conn.commit()


def load_go_lead(conn: sqlite3.Connection, lead_id: int | None = None) -> dict:
    ensure_queue_schema(conn)
    where = "q.lead_id=?" if lead_id is not None else "q.status='READY' AND COALESCE(q.lp_status, 'PENDING') IN ('PENDING','ERROR')"
    params = (lead_id,) if lead_id is not None else ()
    row = conn.execute(
        f"""
        SELECT
          q.queue_id, q.lead_id, q.campaign_id, q.company_name, q.website_url,
          q.status, q.lp_status, q.lp_url,
          l.category, l.area,
          d.decision,
          i.intelligence_json
        FROM sales_queue q
        JOIN leads l ON l.lead_id=q.lead_id
        JOIN screening_decisions d ON d.lead_id=q.lead_id
        LEFT JOIN lead_discovery_intelligence i ON i.lead_id=q.lead_id
        WHERE {where}
        ORDER BY q.queue_id
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        raise SystemExit("No GO lead available in sales_queue.")
    names = [
        "queue_id","lead_id","campaign_id","company_name","website_url",
        "status","lp_status","lp_url","category","area","decision","intelligence_json"
    ]
    item = dict(zip(names, row))
    if item["decision"] != "GO":
        raise SystemExit(f"Lead {item['lead_id']} is not GO: {item['decision']}")
    try:
        item["intelligence"] = json.loads(item["intelligence_json"] or "{}")
    except json.JSONDecodeError:
        item["intelligence"] = {}
    return item


def fallback_copy(lead: dict) -> dict:
    intel = lead["intelligence"]
    company = clean(intel.get("company_name")) or clean(lead.get("company_name")) or "この店舗"
    business = clean(intel.get("business_type")) or clean(lead.get("category")) or "店舗"
    area = clean(intel.get("area")) or clean(lead.get("area")) or ""
    strengths = intel.get("strengths") or []
    opportunities = intel.get("lp_opportunities") or []
    strength = clean(strengths[0]) if strengths else None
    opportunity = clean(opportunities[0]) if opportunities else None
    return {
        "eyebrow": f"{company}様向け Path-Flow診断",
        "headline": f"{company}の魅力を、来店につながる導線へ。",
        "subheadline": (
            f"{area}の{business}としての強みを活かし、問い合わせ・予約までの流れを"
            "分かりやすく整える個別提案です。"
        ),
        "diagnosis": opportunity or "現在のWeb導線を整理し、見込み客が次の行動を選びやすい構成へ改善する余地があります。",
        "strength": strength or "公開情報から確認できる店舗独自の特徴を訴求軸として活用します。",
    }


def generate_copy(lead: dict, ollama_url: str, model: str) -> dict:
    intel = lead["intelligence"]
    prompt = f"""You write concise Japanese landing-page copy for Path-Flow.
Return JSON only. Never invent facts. Use only the supplied lead intelligence.
Required keys: eyebrow, headline, subheadline, diagnosis, strength.
Lead:
{json.dumps({
    "company_name": lead.get("company_name"),
    "category": lead.get("category"),
    "area": lead.get("area"),
    "website_url": lead.get("website_url"),
    "intelligence": intel,
}, ensure_ascii=False)}
"""
    try:
        res = requests.post(
            ollama_url.rstrip("/") + "/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=120,
        )
        res.raise_for_status()
        data = json.loads(res.json().get("response") or "{}")
        required = {"eyebrow","headline","subheadline","diagnosis","strength"}
        if not isinstance(data, dict) or not required.issubset(data):
            raise ValueError("Incomplete Ollama LP copy")
        return data
    except Exception:
        return fallback_copy(lead)


def render_html(template: str, lead: dict, copy: dict) -> str:
    company = clean(lead["intelligence"].get("company_name")) or clean(lead.get("company_name")) or "店舗"
    title = f"{company}様向け Path-Flow 個別提案 | Nexccess"
    description = clean(copy.get("subheadline")) or "Path-Flow 個別提案"

    rendered = template
    rendered = rendered.replace(
        "<title>生成AI活用型 事前診断・集客・予約最適化システム | 合同会社Nexcess</title>",
        f"<title>{html.escape(title)}</title>",
        1,
    )
    rendered = rendered.replace(
        '<meta name="description" content="集客からAI事前診断、予約確定まで一体化。営業・受付業務を自動化する販売業務支援システム。まず無料で適合診断を受けてください。">',
        f'<meta name="description" content="{html.escape(description, quote=True)}">',
        1,
    )
    rendered = rendered.replace(
        '<div class="hero-eyebrow">生成AI × 販売業務支援システム</div>',
        f'<div class="hero-eyebrow">{html.escape(str(copy["eyebrow"]))}</div>',
        1,
    )
    rendered = rendered.replace(
        '<h1 class="hero-title">\n    集客から予約確定まで、<em>AIが自動で動かす。</em>\n  </h1>',
        f'<h1 class="hero-title">\n    {html.escape(str(copy["headline"]))}\n  </h1>',
        1,
    )
    rendered = rendered.replace(
        '<p class="hero-sub">事前診断・顧客スコアリング・予約連動を一体化。営業・受付の工数を削減しながら、成約率を高めます。</p>',
        f'<p class="hero-sub">{html.escape(str(copy["subheadline"]))}</p>',
        1,
    )

    marker = "</section>\n\n<!-- ─── PAIN"
    personalized = f"""
  <div class="section-inner" style="margin-top:2rem;padding-bottom:1rem">
    <div class="section-label">PERSONALIZED DIAGNOSIS</div>
    <p style="max-width:760px;color:var(--muted)">{html.escape(str(copy["diagnosis"]))}</p>
    <p style="max-width:760px;margin-top:.75rem">{html.escape(str(copy["strength"]))}</p>
  </div>
</section>

<!-- ─── PAIN"""
    if marker in rendered:
        rendered = rendered.replace(marker, personalized, 1)

    return rendered


def generate(db: Path, lead_id: int | None, template: Path, output_root: Path, ollama_url: str, model: str) -> dict:
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")
    if not template.exists():
        raise SystemExit(f"Template not found: {template}")

    conn = sqlite3.connect(db)
    try:
        lead = load_go_lead(conn, lead_id)
        copy = generate_copy(lead, ollama_url, model)
        rendered = render_html(template.read_text(encoding="utf-8"), lead, copy)
        relative = Path("generated") / str(lead["lead_id"]) / "index.html"
        output = REPO_ROOT / relative if output_root == DEFAULT_OUTPUT else output_root / str(lead["lead_id"]) / "index.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        ts = now_iso()
        conn.execute(
            """
            UPDATE sales_queue
            SET lp_status='GENERATED', lp_path=?, generated_at=?, lp_version=?,
                deploy_status='PENDING', last_error=NULL
            WHERE queue_id=?
            """,
            (relative.as_posix(), ts, VERSION, lead["queue_id"]),
        )
        conn.commit()
        return {
            "lead_id": lead["lead_id"],
            "campaign_id": lead["campaign_id"],
            "company_name": lead["company_name"],
            "lp_path": relative.as_posix(),
            "lp_status": "GENERATED",
            "deploy_status": "PENDING",
            "version": VERSION,
        }
    finally:
        conn.close()


def verify_public_url(url: str) -> None:
    res = requests.get(url, timeout=30, allow_redirects=True)
    res.raise_for_status()
    if "<html" not in res.text.lower():
        raise RuntimeError("Deployed URL did not return HTML")


def mark_deployed(db: Path, lead_id: int, lp_url: str) -> dict:
    verify_public_url(lp_url)
    conn = sqlite3.connect(db)
    try:
        ensure_queue_schema(conn)
        ts = now_iso()
        cur = conn.execute(
            """
            UPDATE sales_queue
            SET lp_status='DEPLOYED', deploy_status='READY', lp_url=?,
                deployed_at=?, last_error=NULL
            WHERE lead_id=? AND status='READY'
            """,
            (lp_url, ts, lead_id),
        )
        if cur.rowcount != 1:
            raise SystemExit(f"Could not uniquely update sales_queue for lead_id={lead_id}")
        conn.commit()
        return {"lead_id": lead_id, "lp_url": lp_url, "lp_status": "DEPLOYED", "deploy_status": "READY"}
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="GO Lead -> Path-Flow LP generation -> deploy gate bridge")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--lead-id", type=int)
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--mark-deployed", action="store_true")
    p.add_argument("--lp-url")
    args = p.parse_args()

    if args.mark_deployed:
        if args.lead_id is None or not args.lp_url:
            raise SystemExit("--mark-deployed requires --lead-id and --lp-url")
        result = mark_deployed(args.db.resolve(), args.lead_id, args.lp_url)
    else:
        result = generate(
            args.db.resolve(),
            args.lead_id,
            args.template.resolve(),
            args.output_root.resolve(),
            args.ollama_url,
            args.model,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

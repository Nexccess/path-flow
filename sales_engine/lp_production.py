from __future__ import annotations

import argparse
import html
import json
import re
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
CUSTOMER_VOICE_DIR = HERE / "customer_voice"
STORE_INTELLIGENCE_DIR = HERE / "store_intelligence"
MESSAGE_STRATEGY_DIR = HERE / "message_strategy"
VISUAL_DIRECTION_DIR = HERE / "visual_direction"
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
    decision_cols = {r[1] for r in conn.execute("PRAGMA table_info(screening_decisions)")}
    if "decided_at" in decision_cols:
        decision_join = """
        JOIN screening_decisions d
          ON d.rowid = (
            SELECT d2.rowid
            FROM screening_decisions d2
            WHERE d2.lead_id=q.lead_id
            ORDER BY d2.decided_at DESC, d2.rowid DESC
            LIMIT 1
          )
        """
    else:
        decision_join = "JOIN screening_decisions d ON d.lead_id=q.lead_id"

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
        {decision_join}
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


def load_store_intelligence(lead: dict) -> dict:
    path = STORE_INTELLIGENCE_DIR / f"lead_{int(lead['lead_id'])}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_message_strategy(lead: dict) -> dict:
    path = MESSAGE_STRATEGY_DIR / f"lead_{int(lead['lead_id'])}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_visual_direction(lead: dict) -> dict:
    path = VISUAL_DIRECTION_DIR / f"lead_{int(lead['lead_id'])}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_customer_voice(lead: dict) -> dict:
    category = " ".join(
        filter(
            None,
            [
                clean(lead.get("category")),
                clean((lead.get("intelligence") or {}).get("business_type")),
            ],
        )
    )
    if any(term in category for term in ("美容室", "美容院", "ヘアサロン", "hair")):
        path = CUSTOMER_VOICE_DIR / "hair_salon.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def fallback_copy(lead: dict) -> dict:
    intel = lead["intelligence"]
    company = clean(intel.get("company_name")) or clean(lead.get("company_name")) or "この店舗"
    business = clean(intel.get("business_type")) or clean(lead.get("category")) or "店舗"
    area = clean(intel.get("area")) or clean(lead.get("area")) or ""
    strengths = intel.get("strengths") or []
    opportunities = intel.get("lp_opportunities") or []
    strength = clean(strengths[0]) if strengths else None
    opportunity = clean(opportunities[0]) if opportunities else None
    voice = load_customer_voice(lead)
    return {
        "eyebrow": f"{company}様向け Path-Flow診断",
        "headline": f"{company}の魅力を、来店前の不安まで含めて伝える。",
        "subheadline": (
            f"{area}の{business}としての強みと、実際の顧客が来店前に迷いやすい点を"
            "分けて整理した個別提案です。"
        ),
        "diagnosis": opportunity or "店舗の公開情報と顧客側の迷いを分けて整理し、相談・予約前の不安を減らす余地があります。",
        "strength": strength or "公開情報から確認できる店舗独自の特徴だけを訴求軸として扱います。",
        "diagnostic_questions": voice.get("defaultQuestions", []),
        "customer_voice_version": voice.get("version"),
    }


def generate_copy(lead: dict, ollama_url: str, model: str) -> dict:
    intel = lead["intelligence"]
    store_intelligence = load_store_intelligence(lead)
    message_strategy = load_message_strategy(lead)
    visual_direction = load_visual_direction(lead)
    customer_voice = load_customer_voice(lead)
    prompt = f"""You write concise Japanese landing-page copy for Path-Flow.
Return JSON only. Never invent facts.

Strict source separation:
- FACT: supplied lead intelligence and verified store information only.
- CUSTOMER_VOICE: common anxieties/decision criteria from the supplied taxonomy. Never present these as facts about this store.
- INFERENCE: allowed only when explicitly phrased as a possible fit or consideration.
- UNKNOWN: do not fill with guesses.

Required keys:
eyebrow, headline, subheadline, diagnosis, strength, diagnostic_questions, customer_voice_version.

diagnostic_questions must contain 3 to 5 questions selected/adapted from CUSTOMER_VOICE.
Do not claim the store solves a customer anxiety unless store FACT supports that claim.

Lead:
{json.dumps({
    "company_name": lead.get("company_name"),
    "category": lead.get("category"),
    "area": lead.get("area"),
    "website_url": lead.get("website_url"),
    "intelligence": intel,
    "store_intelligence": store_intelligence,
}, ensure_ascii=False)}

STORE_INTELLIGENCE:
{json.dumps(store_intelligence, ensure_ascii=False)}

MESSAGE_STRATEGY:
{json.dumps(message_strategy, ensure_ascii=False)}

VISUAL_DIRECTION:
{json.dumps(visual_direction, ensure_ascii=False)}

CUSTOMER_VOICE:
{json.dumps(customer_voice, ensure_ascii=False)}
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
        if not isinstance(data.get("diagnostic_questions"), list):
            data["diagnostic_questions"] = customer_voice.get("defaultQuestions", [])
        data["customer_voice_version"] = customer_voice.get("version")
        return data
    except Exception:
        return fallback_copy(lead)


def build_customer_voice_section(copy: dict) -> str:
    questions = copy.get("diagnostic_questions") or []
    if not questions:
        raise RuntimeError("Customer Voice questions are required for product-quality LP generation.")

    items = []
    for idx, q in enumerate(questions[:5], 1):
        text = html.escape(str(q.get("text") or q.get("label") or ""))
        options = q.get("options") or []
        option_text = " / ".join(html.escape(str(x)) for x in options[:5])
        items.append(
            '<article class="pain-card">'
            f'<div class="pain-num">{idx:02d}</div>'
            f'<div class="pain-title">{text}</div>'
            f'<p class="pain-desc">{option_text}</p>'
            '</article>'
        )

    return f"""
<section id="personalized">
  <div class="section-inner">
    <div class="section-label">STORE-SPECIFIC DIRECTION</div>
    <h2 class="section-title">この店舗の情報を起点に、<em>来店前の迷い</em>まで整理する。</h2>
    <p style="max-width:820px;color:var(--muted);margin-top:1.25rem">
      {html.escape(str(copy["diagnosis"]))}
    </p>
    <p style="max-width:820px;margin-top:.75rem">
      {html.escape(str(copy["strength"]))}
    </p>
  </div>
</section>

<section id="customer-voice">
  <div class="section-inner">
    <div class="section-label">CUSTOMER VOICE</div>
    <h2 class="section-title">美容室を選ぶ前に、<em>何を確認したいか。</em></h2>
    <p style="max-width:820px;color:var(--muted);margin-top:1.25rem">
      以下は店舗の口コミ事実ではなく、美容室利用者が来店前に迷いやすい論点を整理した質問です。
      店舗固有の強みと混同せず、診断入力として扱います。
    </p>
    <div class="pain-grid" style="margin-top:2.5rem">
      {"".join(items)}
    </div>
  </div>
</section>
"""


def remove_legacy_b2b(rendered: str, customer_voice_html: str) -> str:
    start = re.search(r'<!--\s*─+\s*PAIN.*?-->', rendered, flags=re.S)
    footer = re.search(r'<!--\s*─+\s*FOOTER.*?-->', rendered, flags=re.S)
    if start and footer and footer.start() > start.start():
        rendered = rendered[:start.start()] + customer_voice_html + "\n\n" + rendered[footer.start():]
    else:
        if "</body>" not in rendered:
            raise RuntimeError("HTML body boundary could not be identified.")
        rendered = rendered.replace("</body>", customer_voice_html + "\n</body>", 1)

    overlay = re.search(
        r'<!--\s*═+\s*DIAGNOSIS OVERLAY.*?</div><!-- /#diag-overlay -->',
        rendered,
        flags=re.S,
    )
    if overlay:
        rendered = rendered[:overlay.start()] + rendered[overlay.end():]
    rendered = re.sub(r'<script>.*?</script>', '', rendered, flags=re.S)

    rendered = re.sub(
        r'<div class="nav-links">.*?</div>',
        '<div class="nav-links"><a href="#personalized">店舗別提案</a><a href="#customer-voice">来店前診断</a></div>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<button class="nav-cta".*?</button>',
        '<a class="nav-cta" href="#customer-voice">来店前の希望を整理する</a>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<div class="hero-actions">.*?</div>',
        '<div class="hero-actions">'
        '<a class="btn-primary" href="#customer-voice">来店前の希望を整理する</a>'
        '<a href="#personalized" class="btn-secondary">この店舗向け提案を見る <span class="btn-arrow">→</span></a>'
        '</div>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<div class="hero-stats">.*?</div>\s*<div class="hero-scroll-hint">',
        '<div class="hero-stats">'
        '<div><div class="hero-stat-num">5</div><div class="hero-stat-label">来店前に整理する質問</div></div>'
        '<div><div class="hero-stat-num">VOICE</div><div class="hero-stat-label">顧客の迷いを分離して反映</div></div>'
        '<div><div class="hero-stat-num">STORE</div><div class="hero-stat-label">店舗固有情報を優先</div></div>'
        '</div><div class="hero-scroll-hint">',
        rendered,
        count=1,
        flags=re.S,
    )

    rendered = re.sub(
        r'<footer>.*?</footer>',
        '<footer><div class="footer-logo">Path-Flow</div>'
        '<p class="footer-copy">店舗ごとの公開情報と顧客側の来店前ニーズを分離して構成した個別提案ページです。</p></footer>',
        rendered,
        count=1,
        flags=re.S,
    )
    return rendered


def strip_unused_legacy_css(rendered: str) -> str:
    """Remove CSS blocks for legacy B2B sections no longer present in product LPs."""
    labels = [
        "SOLUTION",
        "FLOW",
        "FEATURES",
        "PRICING",
        "DIAGNOSIS CTA",
        "DIAGNOSIS OVERLAY",
        "LOADING",
        "RESULTS",
        "BOOKING",
        "SUCCESS",
    ]
    for label in labels:
        pattern = (
            r"/\*\s*─+\s*" + re.escape(label) +
            r"\s*─+\s*\*/.*?(?=/\*\s*─+\s*[A-Z][A-Z ]*\s*─+\s*\*/|</style>)"
        )
        rendered = re.sub(pattern, "", rendered, count=1, flags=re.S)

    legacy_selectors = [
        ".features-grid",
        ".solution-layout",
        ".solution-visual",
        ".flow-steps",
        ".flow-arrow",
        ".pricing-card",
        ".pricing-running",
        ".diag-body",
        ".diag-checkbox-grid",
        ".result-header",
        ".result-cards",
        ".result-roi",
        ".booking-grid",
    ]
    for selector in legacy_selectors:
        rendered = re.sub(
            re.escape(selector) + r"\s*\{[^{}]*\}",
            "",
            rendered,
            flags=re.S,
        )

    rendered = re.sub(
        r"(?m)^\s*\.flow-steps::before\s*\{[^{}]*\}\s*$",
        "",
        rendered,
        flags=re.S,
    )

    # Remove trailing selector fragments left inside responsive blocks after
    # legacy selector deletion. They have no declaration body and are invalid.
    rendered = re.sub(r"(?m)^\s*\.pain-grid\s*,?\s*$\n?", "", rendered)
    rendered = re.sub(r"(?m)^\s*\.flow-steps(?:::before)?\s*,?\s*$\n?", "", rendered)
    rendered = re.sub(r"(?m)^\s*[^@{}\n][^{}\n]*,\s*$\n(?=\s*\})", "", rendered)

    rendered = re.sub(r"(?m)^\s*$\n(?=\s*$\n)", "", rendered)
    return rendered


def reorder_visual_sections(rendered: str, section_order: list[str]) -> str:
    """Reorder known product sections without changing their internal markup."""
    known = ["personalized", "customer-voice"]
    requested = [x for x in section_order if x in known]
    if len(requested) < 2 or len(set(requested)) != len(requested):
        return rendered

    blocks: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for section_id in known:
        match = re.search(
            rf'<section id="{re.escape(section_id)}">.*?</section>',
            rendered,
            flags=re.S,
        )
        if not match:
            return rendered
        blocks[section_id] = match.group(0)
        spans.append((match.start(), match.end()))

    start = min(x[0] for x in spans)
    end = max(x[1] for x in spans)
    ordered = "\n\n".join(blocks[x] for x in requested)
    for section_id in known:
        if section_id not in requested:
            ordered += "\n\n" + blocks[section_id]
    return rendered[:start] + ordered + rendered[end:]


def apply_visual_direction(rendered: str, visual: dict) -> str:
    if not visual:
        return rendered

    palette = visual.get("palette") or {}
    shape = visual.get("shape") or {}
    spacing = visual.get("spacing") or {}
    variant = visual.get("variant") or {}
    typography = visual.get("typography") or {}
    hero = visual.get("hero") or {}

    hero_variant = str(variant.get("hero_variant") or "editorial-soft")
    typography_mode = str(variant.get("typography_mode") or "serif-editorial")
    card_style = str(variant.get("card_style") or "soft-rounded")
    density = str(variant.get("density") or spacing.get("density") or "airy")
    section_order = variant.get("section_order") or ["personalized", "customer-voice"]

    rendered = reorder_visual_sections(rendered, list(section_order))

    attrs = (
        f' data-pf-hero="{html.escape(hero_variant)}"'
        f' data-pf-type="{html.escape(typography_mode)}"'
        f' data-pf-cards="{html.escape(card_style)}"'
        f' data-pf-density="{html.escape(density)}"'
    )
    rendered = re.sub(r"<body(?![^>]*data-pf-hero)([^>]*)>", lambda m: f"<body{m.group(1)}{attrs}>", rendered, count=1)

    bg = palette.get("background", "#F5F1EA")
    surface = palette.get("surface", "#FFFDFC")
    surface_alt = palette.get("surface_alt", "#ECE5DA")
    text = palette.get("text", "#211F1C")
    muted = palette.get("muted", "#746E67")
    accent = palette.get("accent", "#6D5B8C")
    accent_soft = palette.get("accent_soft", "#D8CFE7")
    line = palette.get("line", "#CFC5B8")
    black_frame = palette.get("black_frame", "#1C1B1A")
    radius = shape.get("radius", "16px")
    border = shape.get("border", "1px solid rgba(33,31,28,.14)")
    shadow = shape.get("shadow", "0 18px 50px rgba(33,31,28,.08)")
    section_y = spacing.get("section_y", "clamp(72px, 9vw, 120px)")
    content_max = spacing.get("content_max", "1120px")

    if hero_variant == "technical-grid":
        hero_background = (
            f"linear-gradient(180deg,{surface} 0%,{bg} 100%)"
        )
        hero_bg = (
            "linear-gradient(rgba(62,111,120,.055) 1px, transparent 1px),"
            "linear-gradient(90deg, rgba(62,111,120,.055) 1px, transparent 1px),"
            f"linear-gradient(180deg,{surface} 0%,{bg} 100%)"
        )
        hero_extra = """
#hero { align-items:stretch; }
.hero-title { max-width:900px; font-weight:650; }
.hero-sub { max-width:760px; }
.hero-grid { opacity:.28; background-size:48px 48px; mask-image:none; }
.hero-stats { border-top:1px solid var(--border); padding-top:1.1rem; }
"""
    else:
        hero_background = f"linear-gradient(145deg,{surface} 0%,{bg} 56%,{surface_alt} 100%)"
        hero_bg = (
            f"radial-gradient(ellipse 45% 55% at 78% 30%, {accent_soft}55 0%, transparent 70%),"
            f"radial-gradient(ellipse 55% 65% at 20% 85%, {line}55 0%, transparent 70%),"
            f"linear-gradient(145deg,{surface} 0%,{bg} 100%)"
        )
        hero_extra = """
.hero-title { max-width:780px; letter-spacing:-.02em; }
.hero-sub { max-width:650px; }
.hero-grid { opacity:.12; }
"""

    if typography_mode == "sans-technical":
        heading_family = "'DM Sans','Noto Sans JP',sans-serif"
        title_weight = "650"
        label_spacing = ".14em"
    else:
        heading_family = "'Noto Serif JP',serif"
        title_weight = "700"
        label_spacing = ".18em"

    if card_style == "structured":
        card_radius = "4px"
        card_shadow = "none"
        card_border = f"1px solid {line}"
        card_extra = "border-left:3px solid var(--gold);"
    else:
        card_radius = radius
        card_shadow = shadow
        card_border = border
        card_extra = ""

    if density == "compact":
        card_padding = "1.7rem"
        grid_gap = ".75rem"
        section_scale = ".82"
    elif density == "clean":
        card_padding = "2rem"
        grid_gap = "1rem"
        section_scale = ".92"
    else:
        card_padding = "2.5rem"
        grid_gap = "1rem"
        section_scale = "1"

    css = f"""
<style id="pathflow-visual-direction">
:root {{
  --navy: {bg};
  --navy-mid: {surface};
  --navy-light: {surface_alt};
  --gold: {accent};
  --gold-dim: {line};
  --gold-light: {accent_soft};
  --white: {text};
  --muted: {muted};
  --border: {line};
  --glass: {surface}e6;
  --crimson: {accent};
  --black-frame: {black_frame};
}}
body {{ background:var(--navy); color:var(--white); }}
body::before {{ opacity:.12; }}
.section-inner {{ max-width:{content_max}; }}
.section-title, .hero-title, .footer-logo {{
  font-family:{heading_family};
  font-weight:{title_weight};
}}
.section-label {{ letter-spacing:{label_spacing}; }}
nav.scrolled {{
  background:{surface}e8;
  backdrop-filter:blur(18px);
}}
.nav-logo, .nav-logo-mark, .section-label {{ color:var(--gold); }}
.nav-logo-mark {{ border-color:var(--gold-dim); }}
#hero {{
  min-height:82vh;
  background:{hero_background};
}}
.hero-bg {{ background:{hero_bg}; }}
{hero_extra}
.btn-primary {{
  background:var(--black-frame);
  color:{surface};
  border-radius:{'6px' if card_style == 'structured' else '999px'};
  padding-inline:1.6rem;
}}
.btn-primary:hover {{ opacity:.9; }}
.btn-secondary, .nav-links a {{ color:var(--muted); }}
.nav-cta {{
  border-color:var(--black-frame);
  color:var(--black-frame);
  border-radius:{'6px' if card_style == 'structured' else '999px'};
}}
.nav-cta:hover {{ background:var(--black-frame); color:{surface}; }}
#personalized, #customer-voice {{
  padding:calc({section_y} * {section_scale}) 0;
}}
#personalized {{ background:{surface}; }}
#customer-voice {{ background:{surface_alt}; }}
#customer-voice .pain-grid {{
  gap:{grid_gap};
  border:0;
}}
#customer-voice .pain-card {{
  background:{surface};
  border:{card_border};
  border-radius:{card_radius};
  box-shadow:{card_shadow};
  padding:{card_padding};
  {card_extra}
}}
#customer-voice .pain-num {{
  font-size:.72rem;
  color:var(--gold);
  margin-bottom:.9rem;
  letter-spacing:.16em;
}}
#customer-voice .pain-title {{
  font-family:{heading_family};
  font-size:1.08rem;
  line-height:1.65;
}}
footer {{ background:var(--black-frame); color:{bg}; }}
.footer-copy {{ color:{line}; }}
</style>
"""
    if "</head>" not in rendered:
        raise RuntimeError("HTML head boundary could not be identified.")
    rendered = rendered.replace("</head>", css + "\n</head>", 1)

    eyebrow = str(hero.get("eyebrow") or "").strip()
    if eyebrow:
        rendered = re.sub(
            r'<div class="hero-eyebrow">.*?</div>',
            f'<div class="hero-eyebrow">{html.escape(eyebrow)}</div>',
            rendered,
            count=1,
            flags=re.S,
        )
    return rendered


def final_qa(rendered: str, lead: dict, copy: dict) -> dict:
    """Fail closed when a generated LP is not ready to become a baseline artifact."""
    errors: list[str] = []
    lead_id = int(lead["lead_id"])
    company = str(lead.get("company_name") or "")
    store_intelligence = load_store_intelligence(lead)
    message_strategy = load_message_strategy(lead)
    visual_direction = load_visual_direction(lead)

    required_fragments = [
        f'<meta name="pathflow-lead-id" content="{lead_id}">',
        company,
        'id="customer-voice"',
        "CUSTOMER VOICE",
        'id="personalized"',
    ]
    if visual_direction:
        required_fragments.append('id="pathflow-visual-direction"')

    missing = [fragment for fragment in required_fragments if fragment not in rendered]
    if missing:
        errors.append("missing required fragments: " + ", ".join(missing))

    question_count = rendered.count('<article class="pain-card">')
    expected_questions = min(5, len(copy.get("diagnostic_questions") or []))
    if expected_questions < 1:
        errors.append("customer voice questions are empty")
    elif question_count != expected_questions:
        errors.append(
            f"customer voice question count mismatch: expected={expected_questions}, actual={question_count}"
        )

    forbidden_terms = [
        "4,500,000",
        "ROI ESTIMATE",
        "適合スコア",
        "企業規模",
        "導入検討時期",
        "生成AI事前診断エンジン",
        "集客から予約確定まで、",
        "#diag-overlay",
        "/* ─── SOLUTION",
        "/* ─── FLOW",
        "/* ─── FEATURES",
        "/* ─── PRICING",
        "/* ─── DIAGNOSIS CTA",
        "/* ─── DIAGNOSIS OVERLAY",
        "/* ─── RESULTS",
        "/* ─── BOOKING",
    ]
    leaked = [term for term in forbidden_terms if term in rendered]
    if leaked:
        errors.append("legacy B2B remnants: " + ", ".join(leaked))

    orphan_patterns = {
        "orphan pain-grid selector": r"(?m)^\s*\.pain-grid\s*,?\s*$",
        "orphan flow selector": r"(?m)^\s*\.flow-steps(?:::before)?\s*,?\s*$",
        "empty selector before media close": r"(?m)^\s*[^@{}][^{}]*,\s*\n\s*\}",
    }
    for label, pattern in orphan_patterns.items():
        if re.search(pattern, rendered):
            errors.append(label)

    if "<body\\1" in rendered:
        errors.append("malformed visual variant body tag")

    if rendered.count("<style") != rendered.count("</style>"):
        errors.append("unbalanced style tags")
    if rendered.count("<section") != rendered.count("</section>"):
        errors.append("unbalanced section tags")

    if lead_id == 9:
        if not store_intelligence:
            errors.append("lead 9 store intelligence missing")
        if not message_strategy:
            errors.append("lead 9 message strategy missing")
        if not visual_direction:
            errors.append("lead 9 visual direction missing")
        if "VIOLET YOKOHAMA / PATH-FLOW PERSONALIZED" not in rendered:
            errors.append("lead 9 visual eyebrow missing")

    if errors:
        raise RuntimeError("FINAL_QA_FAILED: " + " | ".join(errors))

    return {
        "status": "PASS",
        "question_count": question_count,
        "store_intelligence": bool(store_intelligence),
        "message_strategy": bool(message_strategy),
        "visual_direction": bool(visual_direction),
    }


def render_html(template: str, lead: dict, copy: dict) -> str:
    company = html.escape(lead["company_name"])
    marker = (
        f'<meta name="pathflow-lead-id" content="{int(lead["lead_id"])}">\n'
        f'<meta name="pathflow-company" content="{company}">'
    )

    rendered = template.replace("{{LEAD_ID}}", str(lead["lead_id"]))
    rendered = rendered.replace("{{COMPANY_NAME}}", company)
    rendered = rendered.replace("{{LP_EYEBROW}}", html.escape(str(copy["eyebrow"])))
    rendered = rendered.replace("{{LP_HEADLINE}}", html.escape(str(copy["headline"])))
    rendered = rendered.replace("{{LP_SUBHEADLINE}}", html.escape(str(copy["subheadline"])))
    rendered = rendered.replace("{{LP_DIAGNOSIS}}", html.escape(str(copy["diagnosis"])))
    rendered = rendered.replace("{{LP_STRENGTH}}", html.escape(str(copy["strength"])))

    if '<meta name="pathflow-lead-id"' not in rendered:
        rendered = rendered.replace("</title>", f"</title>\n{marker}", 1)

    rendered = re.sub(
        r'<title>.*?</title>',
        f'<title>{company}様向け Path-Flow 個別提案 | Nexccess</title>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<meta name="description" content=".*?">',
        f'<meta name="description" content="{html.escape(str(copy["subheadline"]))}">',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<div class="hero-eyebrow">.*?</div>',
        f'<div class="hero-eyebrow">{html.escape(str(copy["eyebrow"]))}</div>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<h1 class="hero-title">.*?</h1>',
        f'<h1 class="hero-title">{html.escape(str(copy["headline"]))}</h1>',
        rendered,
        count=1,
        flags=re.S,
    )
    rendered = re.sub(
        r'<p class="hero-sub">.*?</p>',
        f'<p class="hero-sub">{html.escape(str(copy["subheadline"]))}</p>',
        rendered,
        count=1,
        flags=re.S,
    )

    customer_voice_html = build_customer_voice_section(copy)
    rendered = remove_legacy_b2b(rendered, customer_voice_html)
    rendered = strip_unused_legacy_css(rendered)
    rendered = apply_visual_direction(rendered, load_visual_direction(lead))
    final_qa(rendered, lead, copy)
    return rendered


def generate(
    db_path: Path,
    template_path: Path,
    output_root: Path,
    lead_id: int | None,
    ollama_url: str,
    model: str,
) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        lead = load_go_lead(conn, lead_id)
        copy = generate_copy(lead, ollama_url, model)
        template = template_path.read_text(encoding="utf-8")
        rendered = render_html(template, lead, copy)
        qa = final_qa(rendered, lead, copy)
        out_dir = output_root / str(lead["lead_id"])
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "index.html"
        out_file.write_text(rendered, encoding="utf-8")
        rel_path = out_file.relative_to(REPO_ROOT).as_posix()
        conn.execute(
            """
            UPDATE sales_queue
               SET lp_status='GENERATED',
                   lp_path=?,
                   deploy_status='PENDING',
                   generated_at=?,
                   lp_version=?,
                   last_error=NULL
             WHERE queue_id=?
            """,
            (rel_path, now_iso(), VERSION, lead["queue_id"]),
        )
        conn.commit()
        return {
            "lead_id": lead["lead_id"],
            "campaign_id": lead["campaign_id"],
            "company_name": lead["company_name"],
            "lp_path": rel_path,
            "lp_status": "GENERATED",
            "deploy_status": "PENDING",
            "qa_status": qa["status"],
            "qa_question_count": qa["question_count"],
            "quality_status": "BASELINE_FINAL" if int(lead["lead_id"]) == 9 else "QA_PASS",
            "version": VERSION,
        }
    except Exception as exc:
        if "lead" in locals():
            conn.execute(
                "UPDATE sales_queue SET lp_status='ERROR', deploy_status='ERROR', last_error=? WHERE queue_id=?",
                (str(exc)[:1000], lead["queue_id"]),
            )
            conn.commit()
        raise
    finally:
        conn.close()


def verify_deployed(url: str, lead_id: int, company_name: str) -> None:
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    body = res.text
    expected_marker = f'<meta name="pathflow-lead-id" content="{int(lead_id)}">'
    if expected_marker not in body or company_name not in body:
        raise RuntimeError(f"Deployed page does not match lead_id={lead_id}")


def mark_deployed(db_path: Path, lead_id: int, lp_url: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        ensure_queue_schema(conn)
        row = conn.execute(
            "SELECT queue_id, company_name FROM sales_queue WHERE lead_id=? ORDER BY queue_id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        if not row:
            raise SystemExit(f"Lead {lead_id} is not in sales_queue")
        queue_id, company_name = row
        verify_deployed(lp_url, lead_id, company_name)
        conn.execute(
            """
            UPDATE sales_queue
               SET lp_status='DEPLOYED',
                   deploy_status='READY',
                   lp_url=?,
                   deployed_at=?,
                   last_error=NULL
             WHERE queue_id=?
            """,
            (lp_url, now_iso(), queue_id),
        )
        conn.commit()
        return {"lead_id": lead_id, "lp_url": lp_url, "deploy_status": "READY"}
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate or verify a Path-Flow LP for one GO lead")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--lead-id", type=int)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--mark-deployed", action="store_true")
    p.add_argument("--lp-url")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.mark_deployed:
        if args.lead_id is None or not args.lp_url:
            raise SystemExit("--mark-deployed requires --lead-id and --lp-url")
        result = mark_deployed(args.db, args.lead_id, args.lp_url)
    else:
        result = generate(
            args.db,
            args.template,
            args.output,
            args.lead_id,
            args.ollama_url,
            args.model,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

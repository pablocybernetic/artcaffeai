"""
research_agent.py
-----------------
Analyses platform performance data and brand context to surface
3-5 actionable content opportunities per concept.

Approach:
  1. Pre-fetch platform snapshots (Python → Supabase)
  2. Pre-fetch recent briefs (Python → Supabase)
  3. Run web research — DuckDuckGo searches for current Nairobi/Kenya trends
  4. Single Claude Sonnet call with all context (data + web research)
  5. Parse JSON response → store in research_briefs
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

from supabase import Client
from brand_context import get_active

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a senior data-driven marketing strategist for Artcaffe, a premium café brand in Nairobi, Kenya.

Your job is to identify the most compelling content opportunities for the next 1-2 weeks by combining three inputs:
  1. Artcaffe's own platform performance data (BigQuery/GA4/paid)
  2. Competitor and global brand intelligence (what's working in the market RIGHT NOW)
  3. Current Nairobi/Kenya cultural moments and events

── PERFORMANCE DATA ──
You will receive up to 52 weeks of historical weekly data. Use it to:
- Identify seasonal patterns (school term starts, Kenyan public holidays, end-of-month spend cycles)
- Spot year-over-year or quarter-over-quarter trends
- Flag historically high/low performance weeks to pre-empt or capitalise on

── COMPETITIVE INTELLIGENCE ──
You will receive research on local competitors (Java House, K-Krew, Dormans, Brew Bistro) and global
reference brands (Starbucks, Blue Tokai, Tim Hortons, % Arabica). Use it to:
- Identify formats and content angles that are gaining traction (Reels > static, storytelling > product shots)
- Spot gaps — topics/formats competitors are NOT doing that Artcaffe could own
- Identify trends all players are following that Artcaffe should participate in NOW
- Flag specific content types driving the most engagement (behind-the-scenes, origin stories, ASMR brewing, etc.)
- Learn from their most successful post structures without copying them

── OUTPUT FORMAT ──
Output ONLY a JSON object with this exact shape — no markdown fences, no prose:
{
  "summary": "2-3 sentence overview of current performance + key competitive landscape insight",
  "competitive_landscape": "1-2 sentences on what competitors and global brands are doing right now that Artcaffe must respond to or differentiate from",
  "opportunities": [
    {
      "signal": "specific data point or trend observation driving this opportunity",
      "competitive_context": "what competitors/global brands are doing in this space — or the gap they are NOT filling",
      "opportunity": "clear description of the content opportunity",
      "content_angle": "the specific angle or message the content should take",
      "hook": "a compelling opening line or hook for the content",
      "platform": "instagram_organic | meta_ads | linkedin_organic | google_ads",
      "format": "Post | Carousel | Reel | Story | Ad | Article",
      "priority": "high | medium | low",
      "rationale": "why this opportunity fits Artcaffe's brand and will resonate with the Nairobi audience"
    }
  ]
}

Rules:
- Every opportunity must be grounded in at least one data signal OR one competitive/trend signal — never invent.
- Opportunities backed by BOTH own data AND competitive intelligence are the highest priority.
- Identify format gaps: if competitors dominate static posts but ignore Reels, that is an opportunity.
- Avoid repeating angles already covered in recent briefs OR previous research runs.
- Factor in Kenyan culture, Nairobi food scene, and seasonal moments where relevant.
- Return 4-6 opportunities. Quality over quantity.
- Return ONLY the JSON object.\
"""


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Web research (DuckDuckGo — free, no API key required)
# ---------------------------------------------------------------------------
def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Run a single DuckDuckGo text search. Returns [] on any failure."""
    try:
        from duckduckgo_search import DDGS  # noqa: PLC0415
        with DDGS() as ddgs:
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": (r.get("body") or "")[:300],
                    "url": r.get("href", ""),
                }
                for r in ddgs.text(query, max_results=max_results, timelimit="m")
            ]
    except Exception:
        return []


def _fetch_web_research(today: date) -> str:
    """
    Run 10 targeted searches in parallel covering competitors, global reference brands,
    format/engagement trends, and current Nairobi moments.
    Organised into four labelled sections in the prompt.
    Returns empty string if all searches fail (graceful degradation).
    """
    month_year = today.strftime("%B %Y")
    year = today.strftime("%Y")

    queries = {
        # ── Local competitors ──────────────────────────────────────────────
        "Local competitor: Java House": f"Java House Kenya Instagram Facebook content strategy posts {year}",
        "Local competitor: K-Krew & others": f"K-Krew Dormans Brew Bistro Kenya café social media marketing {year}",

        # ── Global reference brands ────────────────────────────────────────
        "Global brand: Starbucks social strategy": f"Starbucks Instagram Reels content strategy most viral posts {year}",
        "Global brand: Blue Tokai & specialty coffee": f"Blue Tokai coffee specialty café Instagram storytelling content {year}",
        "Global brand: % Arabica / Tim Hortons": f"Arabica coffee Tim Hortons café social media content format trends {year}",

        # ── Format & engagement trends ─────────────────────────────────────
        "Instagram format trends": f"Instagram Reels carousel engagement food café brands best performing formats {month_year}",
        "Content type trends (food & storytelling)": f"food brand storytelling behind-the-scenes origin story content Instagram engagement {year}",
        "TikTok & short-form café content": f"café coffee TikTok viral content ASMR brewing barista trends {month_year}",

        # ── What's happening NOW ───────────────────────────────────────────
        "Nairobi food scene now": f"Nairobi restaurant café food scene new openings trends {month_year}",
        "Kenya events & cultural moments": f"Nairobi Kenya events cultural moments calendar {month_year}",
    }

    # Group labels into sections for clean prompt formatting
    SECTIONS = {
        "LOCAL COMPETITORS — what Artcaffe's direct rivals are posting and doing": [
            "Local competitor: Java House",
            "Local competitor: K-Krew & others",
        ],
        "GLOBAL REFERENCE BRANDS — formats and angles from world-class café brands": [
            "Global brand: Starbucks social strategy",
            "Global brand: Blue Tokai & specialty coffee",
            "Global brand: % Arabica / Tim Hortons",
        ],
        "FORMAT & ENGAGEMENT TRENDS — what content types are gaining traction RIGHT NOW": [
            "Instagram format trends",
            "Content type trends (food & storytelling)",
            "TikTok & short-form café content",
        ],
        "NAIROBI NOW — local moments, events, and cultural signals to tap into": [
            "Nairobi food scene now",
            "Kenya events & cultural moments",
        ],
    }

    results_by_label: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_ddg_search, q, 6): label for label, q in queries.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                results_by_label[label] = future.result()
            except Exception:
                results_by_label[label] = []

    lines = ["\nCOMPETITIVE & MARKET INTELLIGENCE (use to ground opportunities in what's working NOW):"]
    any_results = False

    for section_title, labels in SECTIONS.items():
        section_lines = []
        for label in labels:
            hits = results_by_label.get(label, [])
            if not hits:
                continue
            section_lines.append(f"\n  [{label}]")
            for r in hits:
                if r.get("title") or r.get("snippet"):
                    section_lines.append(f"    • {r['title']}: {r['snippet']}")
        if section_lines:
            lines.append(f"\n── {section_title} ──")
            lines.extend(section_lines)
            any_results = True

    if not any_results:
        return ""
    return "\n".join(lines)


def _fetch_snapshots(sb: Client, concept_id: str, days: int = 365) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    res = (
        sb.table("platform_data_snapshots")
        .select("platform,snapshot_date,summary_json")
        .eq("concept_id", concept_id)
        .gte("snapshot_date", since)
        .order("snapshot_date", desc=True)
        .limit(120)
        .execute()
    )
    return res.data or []


def _fetch_recent_briefs(sb: Client, concept_id: str, days: int = 30) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        sb.table("content_briefs")
        .select("content_angle,hook,platform,created_at")
        .eq("concept_id", concept_id)
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )
    return res.data or []


def _build_snapshot_section(snapshots: list[dict]) -> str:
    if not snapshots:
        return "PLATFORM DATA: No recent snapshots available."

    # Group by platform so we can show recent detail + historical trend table
    from collections import defaultdict
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        by_platform[s.get("platform", "unknown")].append(s)

    lines = [f"PLATFORM PERFORMANCE DATA ({len(snapshots)} weekly snapshots, most recent first):"]

    for platform, rows in by_platform.items():
        rows_sorted = sorted(rows, key=lambda r: r.get("snapshot_date", ""), reverse=True)
        lines.append(f"\n── {platform.upper()} ──")

        if platform == "ga4":
            # Last 4 weeks detail
            lines.append("  Recent weeks (detail):")
            for s in rows_sorted[:4]:
                summary = s.get("summary_json") or {}
                totals = summary.get("totals", {})
                channels = summary.get("channels", [])[:2]
                ch_str = ", ".join(f"{c.get('source','?')}({c.get('sessions',0)})" for c in channels)
                lines.append(
                    f"  {s.get('snapshot_date','')}  "
                    f"sessions={totals.get('sessions','?')}  "
                    f"users={totals.get('active_users','?')}  "
                    f"channels=[{ch_str}]"
                )
            # Historical trend table (all remaining weeks)
            if len(rows_sorted) > 4:
                lines.append("  Historical weekly trend (sessions | users | pageviews):")
                for s in rows_sorted[4:]:
                    summary = s.get("summary_json") or {}
                    t = summary.get("totals", {})
                    lines.append(
                        f"  {s.get('snapshot_date','')}  "
                        f"{t.get('sessions','?')} | {t.get('active_users','?')} | {t.get('pageviews','?')}"
                    )

        elif "paid" in platform:
            # Last 4 weeks detail
            lines.append("  Recent weeks (detail):")
            for s in rows_sorted[:4]:
                summary = s.get("summary_json") or {}
                paid = summary.get("paid_ads", {}) or {}
                totals = paid.get("totals", {}) or {}
                txn = (summary.get("transactions", {}) or {}).get("totals", {}) or {}
                by_ch = (paid.get("by_channel") or [])[:2]
                ch_str = ", ".join(
                    f"{c.get('channel','?')}(KES{float(c.get('total_spend') or 0):,.0f})"
                    for c in by_ch
                )
                lines.append(
                    f"  {s.get('snapshot_date','')}  "
                    f"spend=KES{float(totals.get('spend') or 0):,.0f}  "
                    f"impressions={int(totals.get('impressions') or 0):,}  "
                    f"ctr={float(totals.get('ctr') or 0):.2%}  "
                    f"revenue=KES{float(txn.get('total_revenue') or 0):,.0f}  "
                    f"channels=[{ch_str}]"
                )
            # Historical trend table
            if len(rows_sorted) > 4:
                lines.append("  Historical weekly trend (spend KES | revenue KES | impressions):")
                for s in rows_sorted[4:]:
                    summary = s.get("summary_json") or {}
                    paid = (summary.get("paid_ads", {}) or {}).get("totals", {}) or {}
                    txn = (summary.get("transactions", {}) or {}).get("totals", {}) or {}
                    lines.append(
                        f"  {s.get('snapshot_date','')}  "
                        f"{float(paid.get('spend') or 0):,.0f} | "
                        f"{float(txn.get('total_revenue') or 0):,.0f} | "
                        f"{int(paid.get('impressions') or 0):,}"
                    )

    return "\n".join(lines)


def _fetch_recent_research(sb: Client, concept_id: str) -> list[dict]:
    """Fetch previous research runs so Claude avoids repeating the same opportunities."""
    res = (
        sb.table("research_briefs")
        .select("opportunities,summary,created_at")
        .eq("concept_id", concept_id)
        .order("created_at", desc=True)
        .limit(3)
        .execute()
    )
    return res.data or []


def _build_briefs_section(briefs: list[dict]) -> str:
    if not briefs:
        return ""
    lines = ["\nRECENT CONTENT BRIEFS (avoid repeating these angles):"]
    for b in briefs:
        angle = b.get("content_angle") or b.get("hook") or "Untitled"
        platform = b.get("platform", "")
        lines.append(f"  - [{platform}] {angle}")
    return "\n".join(lines)


def _build_previous_research_section(research_runs: list[dict]) -> str:
    if not research_runs:
        return ""
    lines = ["\nPREVIOUS RESEARCH RUNS (you MUST identify completely different opportunities — do not repeat any angle, hook, or platform combination already listed here):"]
    for i, run in enumerate(research_runs, 1):
        lines.append(f"\n  Run {i} ({run.get('created_at','')[:10]}):")
        opps = run.get("opportunities") or []
        for o in opps:
            lines.append(f"    - [{o.get('platform','')}] {o.get('opportunity','')} | Hook: {o.get('hook','')[:60]}")
    return "\n".join(lines)


def run_research(
    *,
    sb: Client,
    anthropic: Any,
    job_id: str,
    concept_id: str,
) -> dict:
    """
    Run the research agent for a concept.
    Returns the saved research_brief row dict.
    """
    # 1. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(
            f"No active brand context for concept_id={concept_id}. "
            "Upload and process brand guidelines first."
        )
    brand_context = ctx.get("context_json") or ctx

    # 2. Fetch data + web research in parallel
    today = date.today()
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_snapshots   = pool.submit(_fetch_snapshots,       sb, concept_id)
        f_briefs      = pool.submit(_fetch_recent_briefs,   sb, concept_id)
        f_research    = pool.submit(_fetch_recent_research, sb, concept_id)
        f_web         = pool.submit(_fetch_web_research,    today)

        snapshots        = f_snapshots.result()
        recent_briefs    = f_briefs.result()
        previous_research = f_research.result()
        web_research     = f_web.result()

    print(f"[research_agent] snapshots={len(snapshots)} briefs={len(recent_briefs)} "
          f"prev_runs={len(previous_research)} web_results={'yes' if web_research else 'none'}",
          flush=True)

    # 3. Build prompt
    user_parts = [
        "BRAND CONTEXT (JSON):",
        json.dumps(brand_context, indent=2),
        "",
        _build_snapshot_section(snapshots),
        _build_briefs_section(recent_briefs),
        _build_previous_research_section(previous_research),
        web_research,
        "",
        f"Today is {today.isoformat()}. Identify 4-6 HIGH-IMPACT content opportunities that are COMPLETELY DIFFERENT from any previously identified above.\n\nUse the competitive intelligence to:\n  1. Identify formats competitors dominate — and where Artcaffe can go further\n  2. Spot content angles NO local competitor is doing (Artcaffe-owned territory)\n  3. Identify global trends from reference brands that should be adapted for Nairobi NOW\n  4. Flag what is driving the highest engagement in the category right now (Reels, ASMR, storytelling, etc.)\n\nEvery opportunity must state its competitive context — what are rivals doing, and how does Artcaffe differentiate or lead?",
    ]
    user_message = "\n".join(p for p in user_parts if p)

    # 4. Single Claude call
    response = anthropic.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        timeout=120.0,
    )
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", "") == "text"
    ).strip()

    if not raw:
        raise RuntimeError(
            f"Claude returned no text. stop_reason={response.stop_reason}"
        )

    # 5. Parse
    parsed = _parse_json(raw)
    opportunities = parsed.get("opportunities", [])
    summary = parsed.get("summary", "")

    # 6. Persist
    row_id = str(uuid.uuid4())
    period_start = (date.today() - timedelta(days=28)).isoformat()
    row = {
        "id": row_id,
        "concept_id": concept_id,
        "opportunities": opportunities,
        "summary": summary,
        "job_id": job_id,
        "week_start": period_start,
        "created_at": _now(),
    }
    res = sb.table("research_briefs").insert(row).execute()
    return res.data[0] if res.data else row

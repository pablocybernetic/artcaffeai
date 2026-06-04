"""
research_agent.py
-----------------
Analyses platform performance data and brand context to surface
3-5 actionable content opportunities per concept.

Simple, reliable approach:
  1. Pre-fetch platform snapshots (Python → Supabase)
  2. Pre-fetch recent briefs (Python → Supabase)
  3. Single Claude Sonnet call with all context
  4. Parse JSON response → store in research_briefs
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from supabase import Client
from brand_context import get_active

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a senior data-driven marketing strategist for Artcaffe, a premium café brand in Nairobi, Kenya.

Analyse the platform performance data and brand context provided, then identify the most compelling content opportunities for the next 1-2 weeks.

Output ONLY a JSON object with this exact shape — no markdown fences, no prose:
{
  "summary": "2-3 sentence overview of current performance and the key theme across opportunities",
  "opportunities": [
    {
      "signal": "specific data point or observation that motivates this opportunity",
      "opportunity": "clear description of the content opportunity",
      "content_angle": "the specific angle or message the content should take",
      "hook": "a compelling opening line or hook for the content",
      "platform": "instagram_organic | meta_ads | linkedin_organic | google_ads",
      "format": "Post | Carousel | Reel | Story | Ad | Article",
      "priority": "high | medium | low",
      "rationale": "why this opportunity fits the brand and will resonate with the audience"
    }
  ]
}

Rules:
- Base every opportunity on specific data signals — never invent metrics.
- Avoid repeating angles already covered in recent briefs.
- Each opportunity must align with the brand voice and at least one messaging pillar.
- Factor in Kenyan culture, Nairobi food scene, and seasonal moments where relevant.
- Return 3-5 opportunities. Quality over quantity.
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


def _fetch_snapshots(sb: Client, concept_id: str, days: int = 28) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    res = (
        sb.table("platform_data_snapshots")
        .select("platform,snapshot_date,summary_json")
        .eq("concept_id", concept_id)
        .gte("snapshot_date", since)
        .order("snapshot_date", desc=True)
        .limit(20)
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
    lines = ["PLATFORM PERFORMANCE DATA (last 28 days):"]
    for s in snapshots:
        platform = s.get("platform", "unknown")
        snap_date = s.get("snapshot_date", "")
        summary = s.get("summary_json") or {}
        lines.append(f"\n[{platform.upper()} — {snap_date}]")
        if platform == "ga4" and summary:
            totals = summary.get("totals", {})
            lines.append(
                f"  Sessions: {totals.get('sessions', 'N/A')} | "
                f"Users: {totals.get('active_users', 'N/A')} | "
                f"Pageviews: {totals.get('pageviews', 'N/A')}"
            )
            channels = summary.get("channels", [])[:3]
            if channels:
                lines.append(
                    "  Top channels: "
                    + ", ".join(f"{c.get('source','?')} ({c.get('sessions', 0)} sessions)" for c in channels)
                )
            top_pages = summary.get("top_pages", [])[:3]
            if top_pages:
                lines.append(
                    "  Top pages: "
                    + ", ".join(f"{p.get('page_url','?')} ({p.get('pageviews',0)} views)" for p in top_pages)
                )
        elif "paid" in platform and summary:
            paid = summary.get("paid_ads", {})
            totals = paid.get("totals", {})
            lines.append(
                f"  Spend: KES {totals.get('spend', 0):,.0f} | "
                f"Impressions: {totals.get('impressions', 0):,} | "
                f"CTR: {totals.get('ctr', 0):.2%}"
            )
            by_channel = paid.get("by_channel", [])[:3]
            if by_channel:
                lines.append(
                    "  By channel: "
                    + ", ".join(f"{c.get('channel','?')} KES{c.get('total_spend',0):,.0f}" for c in by_channel)
                )
            txn = summary.get("transactions", {}).get("totals", {})
            if txn.get("total_revenue"):
                lines.append(
                    f"  Revenue: KES {txn.get('total_revenue',0):,.0f} | "
                    f"Transactions: {txn.get('total_transactions',0)}"
                )
    return "\n".join(lines)


def _build_briefs_section(briefs: list[dict]) -> str:
    if not briefs:
        return ""
    lines = ["\nRECENT CONTENT BRIEFS (avoid repeating these angles):"]
    for b in briefs:
        angle = b.get("content_angle") or b.get("hook") or "Untitled"
        platform = b.get("platform", "")
        lines.append(f"  - [{platform}] {angle}")
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

    # 2. Fetch data
    today = date.today()
    snapshots = _fetch_snapshots(sb, concept_id)
    recent_briefs = _fetch_recent_briefs(sb, concept_id)

    # 3. Build prompt
    user_parts = [
        "BRAND CONTEXT (JSON):",
        json.dumps(brand_context, indent=2),
        "",
        _build_snapshot_section(snapshots),
        _build_briefs_section(recent_briefs),
        "",
        f"Today is {today.isoformat()}. Identify 3-5 high-impact content opportunities for the next 1-2 weeks.",
    ]
    user_message = "\n".join(user_parts)

    # 4. Single Claude call
    response = anthropic.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        timeout=60.0,
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

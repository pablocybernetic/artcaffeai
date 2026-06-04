"""
research_agent.py  (v2 — agentic tool-use loop)
-------------------------------------------------
Replaces the single-call approach with an agentic loop that gives Claude
three tools to gather information before synthesising opportunities:

  1. web_search  (server-side, Anthropic-hosted)
     → trends in Nairobi, competitor activity, cultural moments, food scene

  2. get_platform_data  (custom / client-side)
     → BigQuery snapshots stored in platform_data_snapshots

  3. get_recent_briefs  (custom / client-side)
     → recent content_briefs so Claude avoids repeating angles

Claude decides which tools to call and when.  We execute the two custom tools
and feed results back.  The loop ends when Claude emits stop_reason="end_turn"
and writes the final JSON research brief.
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

Your job: identify 3-5 high-impact content opportunities for the next 1-2 weeks.

Workflow — use both tools before synthesising:
1. Call get_platform_data to understand internal performance (what is working, what is not).
2. Call get_recent_briefs to see what angles have been covered recently (avoid repetition).
Then use your knowledge of Kenyan culture, Nairobi food trends, and seasonal moments to enrich the opportunities.

After gathering sufficient data, write ONLY a JSON object in this exact shape:
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
- Base every opportunity on specific data signals from the tools — never invent metrics.
- Avoid repeating angles already covered in recent briefs.
- Each opportunity must align with the brand voice and at least one messaging pillar.
- Return ONLY the JSON object — no prose before or after it.\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _format_snapshots(snapshots: list[dict]) -> str:
    if not snapshots:
        return "No recent platform data available."
    lines = ["Platform performance data (last 28 days):"]
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
                    + ", ".join(
                        f"{c.get('source','?')} ({c.get('sessions', 0)} sessions)"
                        for c in channels
                    )
                )
            top_pages = summary.get("top_pages", [])[:3]
            if top_pages:
                lines.append(
                    "  Top pages: "
                    + ", ".join(
                        f"{p.get('page_url','?')} ({p.get('pageviews',0)} views)"
                        for p in top_pages
                    )
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
                    + ", ".join(
                        f"{c.get('channel','?')} KES{c.get('total_spend',0):,.0f}"
                        for c in by_channel
                    )
                )
            txn = summary.get("transactions", {}).get("totals", {})
            if txn.get("total_revenue"):
                lines.append(
                    f"  Revenue: KES {txn.get('total_revenue',0):,.0f} | "
                    f"Transactions: {txn.get('total_transactions',0)}"
                )
    return "\n".join(lines)


def _format_briefs(briefs: list[dict]) -> str:
    if not briefs:
        return "No recent content briefs found — all angles are available."
    lines = ["Recent content briefs (avoid repeating these angles):"]
    for b in briefs:
        angle = b.get("content_angle") or b.get("hook") or "Untitled"
        platform = b.get("platform", "")
        lines.append(f"  - [{platform}] {angle}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    # 1. Custom: internal platform data
    {
        "name": "get_platform_data",
        "description": (
            "Fetch internal platform performance data from BigQuery snapshots "
            "for this Artcaffe concept. Returns GA4 website traffic data and "
            "paid ads metrics. Call this to understand what content is working "
            "and which channels are driving results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days of history to retrieve (default 28).",
                },
            },
            "required": [],
        },
    },
    # 3. Custom: recent content briefs
    {
        "name": "get_recent_briefs",
        "description": (
            "Fetch recent content briefs created for this Artcaffe concept. "
            "Use this to see what content angles have already been covered "
            "so you can avoid repetition and find fresh opportunities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to look (default 30).",
                },
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_research(
    *,
    sb: Client,
    anthropic: Any,
    job_id: str,
    concept_id: str,
) -> dict:
    """
    Run the agentic research agent for a concept.
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
    today = date.today()

    # 2. Client-side tool executor (handles our two custom tools)
    def _execute_tool(name: str, tool_input: dict) -> str:
        if name == "get_platform_data":
            days = int(tool_input.get("days", 28))
            snapshots = _fetch_snapshots(sb, concept_id, days=days)
            return _format_snapshots(snapshots)
        if name == "get_recent_briefs":
            days = int(tool_input.get("days", 30))
            briefs = _fetch_recent_briefs(sb, concept_id, days=days)
            return _format_briefs(briefs)
        return f"[tool not found: {name}]"

    # 3. Inject brand context into system prompt
    system_with_brand = (
        f"{SYSTEM_PROMPT}\n\n"
        f"BRAND CONTEXT (JSON):\n{json.dumps(brand_context, indent=2)}"
    )

    # 4. Initial user message
    brand_name = (
        brand_context.get("brand_name", "Artcaffe")
        if isinstance(brand_context, dict)
        else "Artcaffe"
    )
    user_message = (
        f"Research content opportunities for {brand_name}. "
        f"Today is {today.isoformat()}. "
        "Use all three tools to gather data, then output the JSON research brief."
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    # 5. Agentic loop
    final_text = ""
    max_iterations = 15  # safety cap

    for iteration in range(max_iterations):
        response = anthropic.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system_with_brand,
            tools=TOOLS,
            messages=messages,
            timeout=60.0,
        )

        # Always append the full assistant content (preserves tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        print(
            f"[research_agent] iteration={iteration} "
            f"stop_reason={response.stop_reason}",
            flush=True,
        )

        # ── Done ──────────────────────────────────────────────────────────────
        if response.stop_reason == "end_turn":
            # Collect all text blocks; the JSON may span multiple blocks
            # or appear after web_search result blocks.
            text_parts = [
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text" and block.text.strip()
            ]
            final_text = "\n".join(text_parts)
            if not final_text:
                # If still empty, ask Claude to output the JSON now
                messages.append({
                    "role": "user",
                    "content": (
                        "You have finished gathering data. "
                        "Now output ONLY the JSON research brief — no other text."
                    ),
                })
                continue  # one more loop iteration to get the JSON
            break

        # ── Server-side tool loop paused (web_search hit iteration limit) ────
        if response.stop_reason == "pause_turn":
            # Re-send to continue the server-side loop
            continue

        # ── Custom tool calls ─────────────────────────────────────────────────
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    result_text = _execute_tool(block.name, block.input)
                    print(
                        f"[research_agent] executed tool={block.name} "
                        f"→ {len(result_text)} chars",
                        flush=True,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        # Unknown stop reason — exit
        break

    if not final_text:
        raise RuntimeError(
            "Research agent did not produce a final JSON response. "
            f"Last stop_reason={response.stop_reason}"  # noqa: F821
        )

    # 6. Parse JSON from final text
    parsed = _parse_json(final_text)
    opportunities = parsed.get("opportunities", [])
    summary = parsed.get("summary", "")

    # 7. Persist to research_briefs
    row_id = str(uuid.uuid4())
    row = {
        "id": row_id,
        "concept_id": concept_id,
        "opportunities": opportunities,
        "summary": summary,
        "job_id": job_id,
        "created_at": _now(),
    }
    res = sb.table("research_briefs").insert(row).execute()
    return res.data[0] if res.data else row

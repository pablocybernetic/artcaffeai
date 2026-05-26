"""
ideation_agent.py
-----------------
Generates on-brand content ideas for a given content brief using Claude Haiku.
Also selects relevant assets from the assets library for each idea.

Workflow:
  1. Load active brand context for the concept.
  2. Fetch the content_briefs row for brief context.
  3. Fetch available assets for the concept.
  4. Build a prompt combining brand context + brief + assets list.
  5. Call Claude and parse the JSON response.
  6. Persist each idea as a row in content_items (with asset_ids).
  7. Update content_briefs status to "in_review".
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from brand_context import get_active

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """\
You are a senior creative strategist for Artcaffe, a premium café brand in Nairobi, Kenya.
Your role is to generate compelling, on-brand content ideas that resonate with the target audience.

Output ONLY a JSON object with this exact shape — no markdown fences, no prose:
{
  "ideas": [
    {
      "title": "short idea title",
      "headline": "attention-grabbing headline for the post",
      "caption": "short social caption (under 150 chars)",
      "body": "longer body copy for the content",
      "channels": ["instagram", "facebook"],
      "rationale": "how this idea maps to the brand voice and pillars",
      "asset_ids": ["uuid-of-best-fit-asset"]
    }
  ]
}

Rules:
- Use the brand's voice tone words. Avoid anything in voice.dont / vocabulary.avoid.
- Reference at least one messaging pillar in every idea's rationale.
- Every idea must feel premium, warm, and distinctly Artcaffe.
- For asset_ids: pick 1-2 asset UUIDs from the AVAILABLE ASSETS list that best fit the idea visually.
  Use exact UUIDs only. If no asset fits well, use an empty array [].
- Return ONLY the JSON object — absolutely no text before or after it.\
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


def _fetch_assets(sb: Client, concept_id: str) -> list[dict]:
    """Fetch image/video assets for this concept to pass to the agent."""
    res = (
        sb.table("assets")
        .select("id,filename,asset_type,public_url,platform")
        .eq("concept_id", concept_id)
        .in_("asset_type", ["image", "video"])
        .order("created_at", desc=True)
        .limit(40)
        .execute()
    )
    return res.data or []


def _build_asset_section(assets: list[dict]) -> str:
    if not assets:
        return ""
    lines = ["", "AVAILABLE ASSETS (pick by exact UUID):"]
    for a in assets:
        name = a.get("filename") or "unnamed"
        atype = a.get("asset_type") or "file"
        platform = a.get("platform") or ""
        lines.append(f"- {a['id']} | {atype} | {name}" + (f" | {platform}" if platform else ""))
    lines.append("")
    return "\n".join(lines)


def run_ideation(
    *,
    sb: Client,
    anthropic: Any,
    job_id: str,
    brief_id: str,
    concept_id: str,
    n: int = 5,
) -> list[dict]:
    # 1. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(
            f"No active brand context found for concept_id={concept_id}. "
            "Process brand guidelines first."
        )
    brand_context = ctx.get("context_json") or ctx

    # 2. Fetch content_briefs row
    brief_res = (
        sb.table("content_briefs")
        .select("id,content_angle,hook,platform,research_summary,market_data,agent_brief")
        .eq("id", brief_id)
        .single()
        .execute()
    )
    if not brief_res.data:
        raise RuntimeError(f"content_briefs row not found: brief_id={brief_id}")
    brief = brief_res.data

    # 3. Fetch assets
    assets = _fetch_assets(sb, concept_id)

    # 4. Build prompt
    brief_text = (
        brief.get("agent_brief")
        or brief.get("content_angle")
        or brief.get("hook")
        or "Generate creative content ideas."
    )
    platform = brief.get("platform") or "instagram"

    user_parts: list[str] = [
        "BRAND CONTEXT (JSON):",
        json.dumps(brand_context, indent=2),
        "",
        f"BRIEF:\n{brief_text}",
        "",
        f"TARGET PLATFORM: {platform}",
    ]

    if brief.get("research_summary"):
        user_parts += ["", f"RESEARCH SUMMARY:\n{brief['research_summary']}"]

    if brief.get("market_data"):
        market_str = json.dumps(brief["market_data"], indent=2) if isinstance(brief["market_data"], dict) else str(brief["market_data"])
        user_parts += ["", f"MARKET DATA:\n{market_str}"]

    asset_section = _build_asset_section(assets)
    if asset_section:
        user_parts.append(asset_section)

    user_parts += ["", f"Produce {n} distinct ideas. Pick real asset UUIDs from the list above for each idea."]
    user_message = "\n".join(user_parts)

    # 5. Call Claude
    response = anthropic.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        timeout=45.0,
    )
    raw = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()

    # 6. Parse
    parsed = _parse_json(raw)
    ideas: list[dict] = parsed.get("ideas", [])

    # Build a set of valid asset IDs for validation
    valid_asset_ids = {a["id"] for a in assets}

    # 7. Insert each idea
    saved: list[dict] = []
    for idea in ideas:
        channels = idea.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]

        # Validate asset_ids — only keep UUIDs that actually exist in our assets table
        raw_asset_ids = idea.get("asset_ids") or []
        asset_ids = [aid for aid in raw_asset_ids if aid in valid_asset_ids]

        row = {
            "id": str(uuid.uuid4()),
            "brief_id": brief_id,
            "job_id": job_id,
            "type": "concept",
            "platform": platform,
            "title": idea.get("title", ""),
            "headline": idea.get("headline", ""),
            "caption": idea.get("caption", ""),
            "body": idea.get("body", ""),
            "channels": channels,
            "rationale": idea.get("rationale", ""),
            "asset_ids": asset_ids,
            "version": 1,
            "status": "draft",
            "created_at": _now(),
            "updated_at": _now(),
        }
        insert_res = sb.table("content_items").insert(row).execute()
        saved.append(insert_res.data[0] if insert_res.data else row)

    # 8. Update content_briefs status
    sb.table("content_briefs").update(
        {"content_status": "in_review", "ideation_job_id": job_id, "updated_at": _now()}
    ).eq("id", brief_id).execute()

    return saved

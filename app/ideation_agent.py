"""
ideation_agent.py
-----------------
Generates on-brand content ideas for a given content brief using Claude Haiku.

Workflow:
  1. Load active brand context for the concept.
  2. Fetch the content_briefs row for brief context.
  3. Build a prompt combining brand context + brief details + research data.
  4. Call Claude and parse the JSON response.
  5. Persist each idea as a row in content_items.
  6. Update content_briefs status to "in_review".
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
      "rationale": "how this idea maps to the brand voice and pillars"
    }
  ]
}

Rules:
- Use the brand's voice tone words. Avoid anything in voice.dont / vocabulary.avoid.
- Reference at least one messaging pillar in every idea's rationale.
- Every idea must feel premium, warm, and distinctly Artcaffe.
- Return ONLY the JSON object — absolutely no text before or after it.\
"""


def _parse_json(text: str) -> Any:
    """Strip optional ```json fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_ideation(
    *,
    sb: Client,
    anthropic: Any,
    job_id: str,
    brief_id: str,
    concept_id: str,
    n: int = 5,
) -> list[dict]:
    """
    Run the ideation agent for a given content brief.

    Returns the list of saved content_items rows.
    Raises RuntimeError if there is no active brand context.
    """
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
        .select(
            "id,content_angle,hook,platform,research_summary,market_data,agent_brief"
        )
        .eq("id", brief_id)
        .single()
        .execute()
    )
    if not brief_res.data:
        raise RuntimeError(f"content_briefs row not found: brief_id={brief_id}")
    brief = brief_res.data

    # 3. Build prompt
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

    research_summary = brief.get("research_summary")
    if research_summary:
        user_parts += ["", f"RESEARCH SUMMARY:\n{research_summary}"]

    market_data = brief.get("market_data")
    if market_data:
        market_str = (
            json.dumps(market_data, indent=2)
            if isinstance(market_data, dict)
            else str(market_data)
        )
        user_parts += ["", f"MARKET DATA:\n{market_str}"]

    user_parts += ["", f"Produce {n} distinct ideas."]
    user_message = "\n".join(user_parts)

    # 4. Call Claude
    response = anthropic.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        timeout=45.0,
    )
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", "") == "text"
    ).strip()

    # 5. Parse JSON
    parsed = _parse_json(raw)
    ideas: list[dict] = parsed.get("ideas", [])

    # 6. Insert each idea into content_items
    saved: list[dict] = []
    for idea in ideas:
        channels = idea.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]

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
            "version": 1,
            "status": "draft",
            "created_at": _now(),
            "updated_at": _now(),
        }
        insert_res = sb.table("content_items").insert(row).execute()
        if insert_res.data:
            saved.append(insert_res.data[0])
        else:
            saved.append(row)

    # 7. Update content_briefs status
    sb.table("content_briefs").update(
        {
            "content_status": "in_review",
            "ideation_job_id": job_id,
            "updated_at": _now(),
        }
    ).eq("id", brief_id).execute()

    return saved

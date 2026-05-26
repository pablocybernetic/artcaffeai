"""
production_agent.py
-------------------
Produces final, publication-ready copy for an approved content concept.

Workflow:
  1. Fetch the content_items row for the approved concept.
  2. COST GATE: raise RuntimeError if status != "approved".
  3. Load active brand context.
  4. Build a prompt combining brand context + approved concept details.
  5. Call Claude Opus and parse the JSON response.
  6. Persist the final copy as a new content_items row (type="final").
  7. Update content_briefs to content_status="in_production".
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from brand_context import get_active

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """\
You are a professional copywriter for Artcaffe, a premium café brand in Nairobi, Kenya.
Your task is to produce polished, publication-ready copy from an approved content concept.

Output ONLY a JSON object with this exact shape — no markdown fences, no extra text:
{
  "headline": "final attention-grabbing headline",
  "caption": "final social media caption (platform-optimised)",
  "body": "full body copy — well-structured paragraphs, ready to publish",
  "cta": "clear call-to-action phrase",
  "hashtags": ["#artcaffe", "#nairobi"],
  "alt_text": "accessibility alt text for the accompanying image",
  "notes": "any production or scheduling notes for the content team"
}

Rules:
- Match the brand voice exactly — premium, warm, community-focused.
- Copy must be ready to paste directly into the platform's composer.
- Hashtags should be relevant, popular, and brand-appropriate.
- Return ONLY the JSON object — absolutely no text before or after it.\
"""


def _parse_json(text: str) -> Any:
    """Strip optional ```json fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_production(
    *,
    sb: Client,
    anthropic: Any,
    job_id: str,
    content_item_id: str,
    concept_id: str,
) -> dict:
    """
    Run the production agent for an approved content concept.

    Returns the saved content_items row (type="final").
    Raises RuntimeError if the item is not approved or not found.
    """
    # 1. Fetch the content_items row
    item_res = (
        sb.table("content_items")
        .select(
            "id,brief_id,status,platform,title,headline,caption,body,"
            "channels,rationale,type"
        )
        .eq("id", content_item_id)
        .single()
        .execute()
    )
    if not item_res.data:
        raise RuntimeError(
            f"content_items row not found: content_item_id={content_item_id}"
        )
    item = item_res.data

    # 2. COST GATE — only process approved items
    if item.get("status") != "approved":
        raise RuntimeError(
            f"Content item {content_item_id} has status='{item.get('status')}'. "
            "Only 'approved' items can be sent to production. "
            "Approve the concept first before running the production agent."
        )

    # 3. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(
            f"No active brand context found for concept_id={concept_id}. "
            "Process brand guidelines first."
        )
    brand_context = ctx.get("context_json") or ctx

    # 4. Build prompt
    platform = item.get("platform") or "instagram"
    concept_details = {
        "title": item.get("title", ""),
        "headline": item.get("headline", ""),
        "caption": item.get("caption", ""),
        "body": item.get("body", ""),
        "rationale": item.get("rationale", ""),
        "platform": platform,
    }

    user_message = (
        "BRAND CONTEXT (JSON):\n"
        + json.dumps(brand_context, indent=2)
        + "\n\nAPPROVED CONCEPT (JSON):\n"
        + json.dumps(concept_details, indent=2)
        + f"\n\nTarget platform: {platform}"
        + "\n\nProduce the final publication-ready copy for this concept."
    )

    # 5. Call Claude Opus
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

    # 6. Parse JSON response
    parsed = _parse_json(raw)

    channels = item.get("channels") or [platform]

    # 7. Insert new content_items row (type="final")
    new_row = {
        "id": str(uuid.uuid4()),
        "brief_id": item["brief_id"],
        "job_id": job_id,
        "type": "final",
        "platform": platform,
        "title": parsed.get("headline", item.get("title", "")),
        "headline": parsed.get("headline", ""),
        "caption": parsed.get("caption", ""),
        "body": parsed.get("body", ""),
        "channels": channels,
        "metadata": {
            "cta": parsed.get("cta", ""),
            "hashtags": parsed.get("hashtags", []),
            "alt_text": parsed.get("alt_text", ""),
            "notes": parsed.get("notes", ""),
            "source_concept_id": content_item_id,
        },
        "version": 1,
        "status": "draft",
        "created_at": _now(),
        "updated_at": _now(),
    }
    insert_res = sb.table("content_items").insert(new_row).execute()
    saved = insert_res.data[0] if insert_res.data else new_row

    # 8. Update content_briefs status
    sb.table("content_briefs").update(
        {
            "content_status": "in_production",
            "production_job_id": job_id,
            "updated_at": _now(),
        }
    ).eq("id", item["brief_id"]).execute()

    return saved

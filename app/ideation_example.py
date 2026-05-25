"""
ideation_example.py
-------------------
Reference example: how to use an active brand_context to generate
on-brand campaign ideas with Anthropic Claude.

Run locally for testing:

    export SUPABASE_URL=...
    export SUPABASE_SERVICE_ROLE_KEY=...
    export ANTHROPIC_API_KEY=...
    python ideation_example.py <concept_id> "Launch a new oat milk latte"
"""

from __future__ import annotations

import json
import os
import sys

from anthropic import Anthropic
from supabase import create_client

from brand_context import get_active

MODEL = os.environ.get("BRAND_MODEL", "claude-sonnet-4-20250514")

SYSTEM_PROMPT = """You are a senior creative strategist. Generate campaign
ideas that strictly follow the brand context supplied by the user.

Output JSON with this shape:
{
  "ideas": [
    {
      "title": string,
      "big_idea": string,
      "headline": string,
      "caption": string,
      "channels": string[],
      "rationale": string  // how this maps to the brand's pillars/voice
    }
  ]
}

Rules:
- Use the brand's voice tone words and avoid anything in voice.dont / vocabulary.avoid.
- Reference at least one messaging pillar in every idea's rationale.
- Return JSON only, no prose."""


def generate_ideas(
    *,
    anthropic: Anthropic,
    brand_context: dict,
    brief: str,
    n: int = 5,
) -> dict:
    user_msg = (
        "BRAND CONTEXT (JSON):\n"
        + json.dumps(brand_context, indent=2)
        + f"\n\nBRIEF:\n{brief}\n\nProduce {n} ideas."
    )
    msg = anthropic.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    body = "".join(
        b.text for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:].strip()
    return json.loads(body)


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python ideation_example.py <concept_id> <brief>")
        sys.exit(1)

    concept_id, brief = sys.argv[1], sys.argv[2]
    sb = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )
    anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    ctx = get_active(sb, concept_id)
    if not ctx:
        print(f"No active brand context for concept {concept_id}.")
        sys.exit(1)

    ideas = generate_ideas(
        anthropic=anthropic, brand_context=ctx["content"], brief=brief
    )
    print(json.dumps(ideas, indent=2))


if __name__ == "__main__":
    main()

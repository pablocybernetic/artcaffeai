"""
publishing_agent.py
-------------------
Pre-publish intelligence for the Publishing Agent.

Skills (same open-source approach as research_agent.py):
  1. DuckDuckGo hashtag research — trending food/café tags for Nairobi
  2. Claude Haiku caption optimisation — adapts content per platform's style/limits

Called by publishing_routes._execute_publish() before each publish.
Falls back gracefully to original content if Claude or DDG are unavailable.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Optional

MODEL = "claude-haiku-4-5-20251001"

# Per-platform style guide injected into the optimisation prompt
PLATFORM_GUIDES: dict[str, dict] = {
    "instagram": {
        "tone": "visual, aspirational, lifestyle-focused",
        "caption_limit": 2200,
        "hashtag_count": 8,
        "style": "Emojis welcome. Hook in the first line. Hashtags at the end.",
    },
    "facebook": {
        "tone": "community, friendly, conversational",
        "caption_limit": 500,
        "hashtag_count": 3,
        "style": "Friendly, no emoji overload. Short paragraphs. 2-3 hashtags max.",
    },
    "linkedin": {
        "tone": "professional, brand story, insightful",
        "caption_limit": 1200,
        "hashtag_count": 3,
        "style": "Professional. Short paragraphs. 3 relevant hashtags at the end.",
    },
    "twitter": {
        "tone": "punchy, engaging, timely",
        "caption_limit": 240,
        "hashtag_count": 2,
        "style": "Under 240 chars (leave room for hashtags). Hook first. Max 2 hashtags.",
    },
    "whatsapp": {
        "tone": "warm, personal, direct — like a message to a friend",
        "caption_limit": 800,
        "hashtag_count": 0,
        "style": "No hashtags. Direct. Warm. End with a clear call-to-action.",
    },
    "google_ads": {
        "tone": "action-oriented, benefit-focused",
        "caption_limit": 90,
        "hashtag_count": 0,
        "style": "Max 90 chars. Lead with the benefit. Strong call-to-action verb.",
    },
}


# ---------------------------------------------------------------------------
# Skill 1: DuckDuckGo hashtag research
# ---------------------------------------------------------------------------

def _ddg_hashtag_search(query: str, max_results: int = 5) -> list[str]:
    """Extract hashtags from DuckDuckGo results for a query."""
    try:
        from duckduckgo_search import DDGS  # noqa: PLC0415
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results, timelimit="m"))
        tags: set[str] = set()
        for r in results:
            for word in ((r.get("body") or "") + " " + (r.get("title") or "")).split():
                w = word.strip(".,!?\"'()").lower()
                if w.startswith("#") and 3 <= len(w) <= 32:
                    tags.add(w)
        return list(tags)[:12]
    except Exception:
        return []


def _fetch_hashtag_pool(headline: str) -> list[str]:
    """Run 2 parallel hashtag searches and combine results."""
    queries = {
        "food_cafe": "trending Instagram hashtags Nairobi café food coffee 2026",
        "brand_topic": f"trending Instagram hashtags Kenya {headline} food brand",
    }
    all_tags: set[str] = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_ddg_hashtag_search, q): label for label, q in queries.items()}
        for future in as_completed(futures):
            try:
                all_tags.update(future.result())
            except Exception:
                pass
    return sorted(all_tags)[:20]


# ---------------------------------------------------------------------------
# Skill 2: Claude Haiku caption optimisation
# ---------------------------------------------------------------------------

def _build_prompt(platform: str, headline: str, original_caption: str, hashtag_pool: list[str]) -> str:
    guide = PLATFORM_GUIDES.get(platform, PLATFORM_GUIDES["instagram"])
    pool_str = " ".join(hashtag_pool) if hashtag_pool else "(none found)"
    return f"""You are a social media copywriter for Artcaffe, a premium café brand in Nairobi, Kenya.

Optimise the content below for {platform.upper()}.

Platform style: {guide['style']}
Tone: {guide['tone']}
Caption character limit: {guide['caption_limit']}
Number of hashtags to include: {guide['hashtag_count']}

Original headline: {headline}
Original caption: {original_caption}
Hashtag pool (pick the most relevant, or create new ones): {pool_str}

Return ONLY a JSON object — no markdown fences, no prose:
{{"caption": "...", "hashtags": ["#tag1", "#tag2"]}}

Rules:
- Caption must fit within {guide['caption_limit']} characters
- Use exactly {guide['hashtag_count']} hashtags (0 for whatsapp and google_ads)
- Keep Artcaffe's premium, warm, Nairobi-native brand voice
- Return ONLY the JSON"""


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _optimize_one(anthropic: Any, platform: str, headline: str, caption: str, hashtag_pool: list[str]) -> dict:
    """Call Claude Haiku to optimise content for one platform. Returns {"caption", "hashtags"}."""
    prompt = _build_prompt(platform, headline, caption, hashtag_pool)
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
        timeout=30.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    parsed = _parse_json(raw)
    return {
        "caption": str(parsed.get("caption") or caption),
        "hashtags": [str(h) for h in (parsed.get("hashtags") or [])],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def optimize_for_platforms(
    anthropic: Optional[Any],
    headline: str,
    caption: str,
    platforms: list[str],
    today: Optional[date] = None,
) -> dict[str, dict]:
    """
    Optimise content for each target platform using Claude + hashtag research.

    Returns {platform: {"caption": str, "hashtags": list[str]}}.
    Always returns a result for every platform — falls back to the original
    caption on any error so publishing is never blocked by the skills layer.
    """
    fallback = {"caption": caption, "hashtags": []}

    if not anthropic or not platforms:
        return {p: fallback for p in platforms}

    # Hashtag research runs once and is shared across all platform optimisations
    hashtag_pool: list[str] = []
    try:
        hashtag_pool = _fetch_hashtag_pool(headline)
        print(f"[publishing_agent] hashtag_pool={len(hashtag_pool)} tags", flush=True)
    except Exception as e:
        print(f"[publishing_agent] hashtag search failed: {e}", flush=True)

    results: dict[str, dict] = {}

    def _task(platform: str) -> tuple[str, dict]:
        try:
            optimised = _optimize_one(anthropic, platform, headline, caption, hashtag_pool)
            return platform, optimised
        except Exception as exc:
            print(f"[publishing_agent] optimise({platform}) failed: {exc}", flush=True)
            return platform, fallback

    with ThreadPoolExecutor(max_workers=min(len(platforms), 4)) as pool:
        for platform, result in pool.map(lambda p: _task(p), platforms):
            results[platform] = result

    # Fill any gaps with fallback
    for p in platforms:
        if p not in results:
            results[p] = fallback

    print(f"[publishing_agent] optimised {len(results)} platforms", flush=True)
    return results

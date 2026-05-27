"""
image_analysis_agent.py
-----------------------
Uses Claude Haiku vision to analyze uploaded marketing images and produce
structured metadata stored on the assets row. The metadata is then used by
the Ideation Agent to select precisely matching assets for content ideas.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import httpx
from supabase import Client

MODEL = "claude-haiku-4-5-20251001"

ANALYSIS_PROMPT = """\
You are an expert visual analyst for Artcaffe, a premium café brand in Nairobi, Kenya.
Analyze this image and return ONLY a JSON object with this exact shape — no markdown fences, no prose:
{
  "description": "one-sentence plain-English description of what the image shows",
  "objects": ["main", "objects", "visible", "in", "the", "image"],
  "scene_type": "indoor|outdoor|studio|product|aerial",
  "mood": "warm|energetic|calm|playful|sophisticated|rustic|minimal|festive",
  "dominant_colors": ["color name or approximate hex"],
  "composition": "portrait|landscape|square|close-up|wide-angle|overhead|flat-lay",
  "photography_style": "editorial|candid|product|lifestyle|flat-lay|detail|architectural",
  "food_items": ["any food or drink items visible — empty list if none"],
  "people_present": true,
  "suitable_platforms": ["instagram", "facebook", "linkedin", "google_ads"],
  "content_themes": ["morning routine", "coffee culture", "socializing", "premium dining"],
  "tags": ["flat", "keyword", "list", "for", "full-text", "search", "max-15"],
  "artcaffe_relevance": "1-sentence note on how this image fits Artcaffe brand"
}
Return ONLY the JSON object. No explanations, no markdown, no extra text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _fetch_image_b64(url: str) -> tuple[str, str]:
    """Download an image URL and return (base64_data, media_type)."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    # Normalize to MIME types accepted by Claude vision API
    if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        content_type = "image/jpeg"
    b64 = base64.standard_b64encode(resp.content).decode()
    return b64, content_type


def analyze_asset(
    *,
    sb: Client,
    anthropic: Any,
    asset_id: str,
) -> dict:
    """
    Fetch an asset row, download the image, run Claude vision analysis,
    then persist structured metadata back to the assets table.

    Returns the metadata dict on success.
    Raises RuntimeError on unrecoverable failure (after updating DB status to 'failed').
    """
    # 1. Fetch asset row
    res = (
        sb.table("assets")
        .select("id,public_url,mime_type,asset_type")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    if not res.data:
        raise RuntimeError(f"Asset not found: {asset_id}")
    asset = res.data

    # 2. Only images are analysable
    if asset.get("asset_type") not in ("image",):
        sb.table("assets").update({"analysis_status": "skipped"}).eq("id", asset_id).execute()
        return {"skipped": True, "reason": "non-image asset"}

    public_url = asset.get("public_url")
    if not public_url:
        sb.table("assets").update({"analysis_status": "failed", "metadata": {"error": "no public_url"}}).eq("id", asset_id).execute()
        raise RuntimeError(f"Asset {asset_id} has no public_url")

    # 3. Download & base64-encode
    try:
        b64_data, media_type = _fetch_image_b64(public_url)
    except Exception as e:
        err = f"Failed to fetch image: {e}"
        sb.table("assets").update({"analysis_status": "failed", "metadata": {"error": err}}).eq("id", asset_id).execute()
        raise RuntimeError(err) from e

    # 4. Call Claude Haiku vision
    try:
        response = anthropic.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        },
                        {"type": "text", "text": ANALYSIS_PROMPT},
                    ],
                }
            ],
            timeout=30.0,
        )
        raw = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        ).strip()
        metadata = _parse_json(raw)
    except Exception as e:
        err = f"Claude analysis failed: {e}"
        sb.table("assets").update({"analysis_status": "failed", "metadata": {"error": err}}).eq("id", asset_id).execute()
        raise RuntimeError(err) from e

    # 5. Persist metadata
    sb.table("assets").update(
        {"metadata": metadata, "analysis_status": "done"}
    ).eq("id", asset_id).execute()

    return metadata

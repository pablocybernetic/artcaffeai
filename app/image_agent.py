"""
image_agent.py
--------------
Generates marketing banner images using an AI image generation API.

Workflow:
  1. Claude generates a detailed, brand-aligned image prompt
  2. Image generation API creates the banner (DALL-E 3 or Ideogram)
  3. Image bytes downloaded and uploaded to Supabase Storage
  4. Asset row created in the assets table
  5. Asset ID appended to the content_item's asset_ids

Env vars:
  OPENAI_API_KEY      — enables DALL-E 3 (primary)
  IDEOGRAM_API_KEY    — enables Ideogram V2 (set IMAGE_PROVIDER=ideogram)
  IMAGE_PROVIDER      — "openai" (default) | "ideogram"
  ASSETS_BUCKET       — storage bucket for generated images (default: "assets")
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from supabase import Client

from brand_context import get_active

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "openai")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
IDEOGRAM_API_KEY = os.environ.get("IDEOGRAM_API_KEY")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "assets")
FALLBACK_BUCKET = os.environ.get("BRAND_BUCKET", "brand-guidelines")

# ---------------------------------------------------------------------------
# Prompt engineering
# ---------------------------------------------------------------------------
PROMPT_SYSTEM = """\
You are an art director for Artcaffe Coffee & Restaurant, a premium café brand in Nairobi, Kenya.
Your task: write a detailed image generation prompt for a marketing banner.

Output ONLY a JSON object — no markdown, no prose:
{
  "prompt": "detailed scene description for the image generator",
  "negative_prompt": "what to avoid",
  "size": "1024x1024"
}

Image style guidelines:
- Mood: warm, inviting, premium, aspirational
- Aesthetic: specialty coffee photography, lifestyle, natural lighting
- Elements: coffee cups, latte art, food, cozy interiors, or Nairobi lifestyle
- Colors: warm earth tones, cream, rich browns, soft neutrals
- NO text, logos, or watermarks in the image (copy is added separately)
- Size: "1024x1024" for square posts, "1792x1024" for landscape banners, "1024x1792" for stories
- Describe a real, photographic scene — not illustration or cartoon style\
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return json.loads(text.strip())


def _make_image_prompt(
    anthropic: Any,
    *,
    brand_context: dict,
    headline: str,
    caption: str,
    platform: str,
) -> dict:
    """Ask Claude to write a detailed image generation prompt."""
    user_msg = (
        "BRAND CONTEXT:\n" + json.dumps(brand_context, indent=2) + "\n\n"
        f"HEADLINE: {headline}\n"
        f"CAPTION: {caption}\n"
        f"PLATFORM: {platform}\n\n"
        "Write the image prompt for a marketing banner that fits this content."
    )
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=500,
        system=PROMPT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        timeout=20.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Image generation providers
# ---------------------------------------------------------------------------
_VALID_DALLE_SIZES = {"1024x1024", "1792x1024", "1024x1792"}


def _generate_dalle(prompt: str, size: str) -> bytes:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    if size not in _VALID_DALLE_SIZES:
        size = "1024x1024"
    r = httpx.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": "dall-e-3", "prompt": prompt, "n": 1, "size": size, "response_format": "url"},
        timeout=90.0,
    )
    r.raise_for_status()
    image_url = r.json()["data"][0]["url"]
    img = httpx.get(image_url, timeout=60.0)
    img.raise_for_status()
    return img.content


def _generate_ideogram(prompt: str, negative_prompt: str, size: str) -> bytes:
    if not IDEOGRAM_API_KEY:
        raise RuntimeError("IDEOGRAM_API_KEY not configured")
    aspect_map = {"1024x1024": "ASPECT_1_1", "1792x1024": "ASPECT_16_9", "1024x1792": "ASPECT_9_16"}
    aspect = aspect_map.get(size, "ASPECT_1_1")
    r = httpx.post(
        "https://api.ideogram.ai/generate",
        headers={"Api-Key": IDEOGRAM_API_KEY, "Content-Type": "application/json"},
        json={"image_request": {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "aspect_ratio": aspect,
            "model": "V_2",
            "style_type": "REALISTIC",
        }},
        timeout=90.0,
    )
    r.raise_for_status()
    image_url = r.json()["data"][0]["url"]
    img = httpx.get(image_url, timeout=60.0)
    img.raise_for_status()
    return img.content


def _pick_provider() -> str:
    if IMAGE_PROVIDER == "ideogram" and IDEOGRAM_API_KEY:
        return "ideogram"
    if OPENAI_API_KEY:
        return "openai"
    if IDEOGRAM_API_KEY:
        return "ideogram"
    raise RuntimeError(
        "No image provider configured. Set OPENAI_API_KEY (DALL-E 3) "
        "or IDEOGRAM_API_KEY + IMAGE_PROVIDER=ideogram."
    )


def _generate_image(prompt: str, negative_prompt: str, size: str) -> tuple[bytes, str]:
    """Generate image bytes. Returns (bytes, provider_name)."""
    provider = _pick_provider()
    if provider == "ideogram":
        return _generate_ideogram(prompt, negative_prompt, size), "ideogram"
    return _generate_dalle(prompt, size), "dalle3"


# ---------------------------------------------------------------------------
# Storage upload
# ---------------------------------------------------------------------------
def _upload_to_storage(sb: Client, image_bytes: bytes, concept_id: str) -> tuple[str, str, str]:
    """Upload image to Supabase Storage. Returns (bucket, storage_path, public_url)."""
    filename = f"banner_{uuid.uuid4().hex[:10]}.png"
    storage_path = f"banners/{concept_id}/{filename}"

    for bucket in [ASSETS_BUCKET, FALLBACK_BUCKET]:
        try:
            sb.storage.from_(bucket).upload(
                storage_path,
                image_bytes,
                file_options={"content-type": "image/png", "upsert": "true"},
            )
            public_url = sb.storage.from_(bucket).get_public_url(storage_path)
            return bucket, storage_path, public_url
        except Exception as e:
            print(f"[image_agent] upload to bucket '{bucket}' failed: {e}", flush=True)

    raise RuntimeError("Could not upload image to any Supabase Storage bucket")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_image_generation(
    *,
    sb: Client,
    anthropic: Any,
    concept_id: str,
    content_item_id: str | None,
    headline: str,
    caption: str,
    platform: str = "instagram",
) -> dict:
    """
    Generate a banner image and save it as an asset.
    Optionally attach it to a content_item by ID.
    Returns the saved asset row.
    """
    # 1. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(f"No active brand context for concept_id={concept_id}")
    brand_context = ctx.get("context_json") or ctx

    # 2. Generate image prompt via Claude
    prompt_data = _make_image_prompt(
        anthropic,
        brand_context=brand_context,
        headline=headline,
        caption=caption,
        platform=platform,
    )
    image_prompt = prompt_data.get("prompt", "").strip()
    negative_prompt = prompt_data.get("negative_prompt", "")
    size = prompt_data.get("size", "1024x1024")

    if not image_prompt:
        raise RuntimeError("Claude returned an empty image prompt")

    print(f"[image_agent] prompt='{image_prompt[:120]}…' size={size}", flush=True)

    # 3. Generate image
    image_bytes, provider = _generate_image(image_prompt, negative_prompt, size)

    # 4. Upload to storage
    bucket, storage_path, public_url = _upload_to_storage(sb, image_bytes, concept_id)

    print(f"[image_agent] uploaded to {bucket}/{storage_path}", flush=True)

    # 5. Create asset row
    filename = storage_path.split("/")[-1]
    asset_row: dict = {
        "concept_id": concept_id,
        "filename": filename,
        "asset_type": "image",
        "platform": platform,
        "public_url": public_url,
        "created_at": _now(),
    }
    insert_res = sb.table("assets").insert(asset_row).execute()
    saved = insert_res.data[0] if insert_res.data else asset_row

    # 6. Attach to content item
    if content_item_id and saved.get("id"):
        item_res = (
            sb.table("content_items")
            .select("asset_ids")
            .eq("id", content_item_id)
            .single()
            .execute()
        )
        if item_res.data:
            existing = item_res.data.get("asset_ids") or []
            sb.table("content_items").update({
                "asset_ids": list({*existing, saved["id"]}),
                "updated_at": _now(),
            }).eq("id", content_item_id).execute()

    return {**saved, "_prompt": image_prompt, "_provider": provider, "_size": size}

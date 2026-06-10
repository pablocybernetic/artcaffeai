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
from image_overlay import overlay_headline

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


def _generate_dalle(prompt: str, size: str, api_key: str) -> bytes:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    if size not in _VALID_DALLE_SIZES:
        size = "1024x1024"
    r = httpx.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "dall-e-3", "prompt": prompt, "n": 1, "size": size, "response_format": "url"},
        timeout=90.0,
    )
    r.raise_for_status()
    image_url = r.json()["data"][0]["url"]
    img = httpx.get(image_url, timeout=60.0)
    img.raise_for_status()
    return img.content


def _generate_ideogram(prompt: str, negative_prompt: str, size: str, api_key: str) -> bytes:
    if not api_key:
        raise RuntimeError("IDEOGRAM_API_KEY not configured")
    aspect_map = {"1024x1024": "ASPECT_1_1", "1792x1024": "ASPECT_16_9", "1024x1792": "ASPECT_9_16"}
    aspect = aspect_map.get(size, "ASPECT_1_1")
    r = httpx.post(
        "https://api.ideogram.ai/generate",
        headers={"Api-Key": api_key, "Content-Type": "application/json"},
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


def _pick_provider(
    key_override: str = "",
    provider_override: str = "",
) -> tuple[str, str]:
    """Returns (provider_name, api_key). Prefers request-level overrides over env vars."""
    # Resolve provider preference
    preferred = provider_override.strip() or IMAGE_PROVIDER

    if preferred == "ideogram":
        key = key_override.strip() or IDEOGRAM_API_KEY or ""
        if key:
            return "ideogram", key
        # fall through to try openai
    # Try openai
    key = (key_override.strip() if preferred == "openai" else "") or OPENAI_API_KEY or ""
    if key:
        return "openai", key
    # Last resort: any ideogram key
    key = IDEOGRAM_API_KEY or ""
    if key:
        return "ideogram", key
    raise RuntimeError(
        "No image provider configured. Set OPENAI_API_KEY (DALL-E 3) "
        "or IDEOGRAM_API_KEY + IMAGE_PROVIDER=ideogram."
    )


def _generate_image(
    prompt: str,
    negative_prompt: str,
    size: str,
    key_override: str = "",
    provider_override: str = "",
) -> tuple[bytes, str]:
    """Generate image bytes. Returns (bytes, provider_name)."""
    provider, api_key = _pick_provider(key_override, provider_override)
    if provider == "ideogram":
        return _generate_ideogram(prompt, negative_prompt, size, api_key), "ideogram"
    return _generate_dalle(prompt, size, api_key), "dalle3"


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
    image_api_key: str = "",
    image_provider_override: str = "",
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
    image_bytes, provider = _generate_image(
        image_prompt, negative_prompt, size,
        key_override=image_api_key,
        provider_override=image_provider_override,
    )

    # 3b. Burn headline text onto the image using brand fonts from storage
    image_bytes = overlay_headline(image_bytes, headline, sb)

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


# ---------------------------------------------------------------------------
# Fallback: select best existing asset when no image provider is configured
# ---------------------------------------------------------------------------
SELECT_SYSTEM = """\
You are an art director for Artcaffe Coffee & Restaurant.
Given a content headline, caption, and a list of available image assets,
pick the single asset that best matches the visual tone and subject.
Output ONLY the UUID of your choice — nothing else.\
"""


def select_best_asset(
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
    When no image generation provider is configured, use Claude to pick
    the best matching existing asset from the library.
    Attaches it to content_item_id and returns the asset row.
    """
    import re  # noqa: PLC0415
    from ideation_agent import _fetch_assets, _build_asset_section  # noqa: PLC0415

    assets = _fetch_assets(sb, concept_id)
    if not assets:
        raise RuntimeError(
            "No assets found in library. Upload images in Assets first, "
            "or add an image API key to generate new banners."
        )

    valid_ids = {a["id"] for a in assets}
    asset_section = _build_asset_section(assets)

    user_msg = (
        f"HEADLINE: {headline}\n"
        f"CAPTION: {caption}\n"
        f"PLATFORM: {platform}\n"
        f"{asset_section}\n"
        "Output ONLY the single best-matching UUID."
    )

    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=60,
        system=SELECT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        timeout=15.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    # Extract any UUID from the response and validate it
    candidates = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw, re.I
    )
    chosen_id = next((uid for uid in candidates if uid in valid_ids), None)

    if not chosen_id:
        chosen_id = assets[0]["id"]  # last resort: most-recent asset

    asset = next(a for a in assets if a["id"] == chosen_id)

    if content_item_id:
        item_res = (
            sb.table("content_items")
            .select("asset_ids")
            .eq("id", content_item_id)
            .single()
            .execute()
        )
        if item_res.data:
            existing = item_res.data.get("asset_ids") or []
            if chosen_id not in existing:
                sb.table("content_items").update({
                    "asset_ids": list({*existing, chosen_id}),
                    "updated_at": _now(),
                }).eq("id", content_item_id).execute()

    return {**asset, "_mode": "selected"}

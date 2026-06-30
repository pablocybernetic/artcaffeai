"""
image_agent.py
--------------
Generates marketing banner images using an AI image generation API.

Workflow (preferred — ideation asset present):
  1. Fetch the existing image attached to the content item during ideation
  2. Claude generates an enhancement prompt describing the subject
  3. Ideogram /remix polishes the photo while preserving the original subject
  4. Text overlay (headline + body) composited via image_overlay
  5. Composited image uploaded to Supabase Storage + asset row created

Workflow (fallback — no ideation asset):
  1. Claude generates a brand-aligned text-to-image prompt
  2. Ideogram /generate creates a fresh banner image
  3–5. Same as above

Env vars:
  IDEOGRAM_API_KEY    — Ideogram V2 API key
  ASSETS_BUCKET       — storage bucket for generated images (default: "generated-assets")
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
from image_overlay import overlay_creative

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
IDEOGRAM_API_KEY = os.environ.get("IDEOGRAM_API_KEY", "")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "generated-assets")
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
# Image generation — Ideogram V2
# ---------------------------------------------------------------------------

_ASPECT_MAP = {"1024x1024": "ASPECT_1_1", "1792x1024": "ASPECT_16_9", "1024x1792": "ASPECT_9_16"}


def _resolve_api_key(key_override: str) -> str:
    key = key_override.strip() or IDEOGRAM_API_KEY
    if not key:
        raise RuntimeError(
            "No image provider configured. Add your Ideogram API key in Settings → AI image generation."
        )
    return key


def _download_image_url(url: str) -> bytes:
    r = httpx.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _generate_ideogram(prompt: str, negative_prompt: str, size: str, api_key: str) -> bytes:
    """Text-to-image via Ideogram V2 /generate."""
    aspect = _ASPECT_MAP.get(size, "ASPECT_1_1")
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
    return _download_image_url(r.json()["data"][0]["url"])


def _remix_ideogram(
    source_bytes: bytes,
    prompt: str,
    negative_prompt: str,
    size: str,
    api_key: str,
    image_weight: int = 70,
) -> bytes:
    """
    Image-to-image remix via Ideogram V2 /remix.
    image_weight: 0–100, higher = closer to the original photo (70 = good balance).
    Sends source image as multipart/form-data.
    """
    import io as _io
    aspect = _ASPECT_MAP.get(size, "ASPECT_1_1")
    image_request = json.dumps({
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "aspect_ratio": aspect,
        "model": "V_2",
        "style_type": "REALISTIC",
        "image_weight": image_weight,
    })
    r = httpx.post(
        "https://api.ideogram.ai/remix",
        headers={"Api-Key": api_key},
        data={"image_request": image_request},
        files={"image_file": ("source.jpg", _io.BytesIO(source_bytes), "image/jpeg")},
        timeout=120.0,
    )
    r.raise_for_status()
    return _download_image_url(r.json()["data"][0]["url"])


def _generate_image(
    prompt: str,
    negative_prompt: str,
    size: str,
    key_override: str = "",
) -> tuple[bytes, str]:
    """Text-to-image via Ideogram V2. Returns (bytes, 'ideogram')."""
    return _generate_ideogram(prompt, negative_prompt, size, _resolve_api_key(key_override)), "ideogram"


# ---------------------------------------------------------------------------
# Ideation asset resolver
# ---------------------------------------------------------------------------

def _get_ideation_asset_url(sb: Client, content_item_id: str) -> str | None:
    """
    Return the public_url of the first non-AI image asset attached to this
    content item (i.e. the photo selected during ideation).
    Returns None if no such asset exists.
    """
    try:
        item_res = (
            sb.table("content_items")
            .select("asset_ids")
            .eq("id", content_item_id)
            .single()
            .execute()
        )
        asset_ids: list = (item_res.data or {}).get("asset_ids") or []
        if not asset_ids:
            return None

        assets_res = (
            sb.table("assets")
            .select("id,public_url,generator")
            .in_("id", asset_ids)
            .in_("asset_type", ["image"])
            .is_("generator", "null")   # human-uploaded / ideation-picked only
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        data = assets_res.data or []
        url = data[0].get("public_url") if data else None
        if url:
            print(f"[image_agent] ideation asset found: {url[:80]}…", flush=True)
        return url
    except Exception as exc:
        print(f"[image_agent] could not resolve ideation asset: {exc}", flush=True)
        return None


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
def _extract_brand_color(brand_context: dict) -> str:
    """Pull primary brand colour hex from context JSON, fallback to Artcaffe green."""
    for key in ("primary_color", "primary_colour"):
        if brand_context.get(key):
            return brand_context[key]
    colors = brand_context.get("brand_colors") or brand_context.get("colors") or []
    if isinstance(colors, list) and colors:
        return colors[0] if isinstance(colors[0], str) else ""
    if isinstance(colors, dict):
        return colors.get("primary") or colors.get("main") or ""
    return ""


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

    # 3. Generate image — prefer remixing the ideation photo; fall back to text-to-image
    api_key = _resolve_api_key(image_api_key)
    source_url = _get_ideation_asset_url(sb, content_item_id) if content_item_id else None

    if source_url:
        try:
            source_bytes = _download_image_url(source_url)
            image_bytes  = _remix_ideogram(source_bytes, image_prompt, negative_prompt, size, api_key)
            provider     = "ideogram-remix"
            print(f"[image_agent] remix successful ({len(image_bytes)//1024} KB)", flush=True)
        except Exception as exc:
            print(f"[image_agent] remix failed ({exc}), falling back to text-to-image", flush=True)
            image_bytes, provider = _generate_image(image_prompt, negative_prompt, size, image_api_key)
    else:
        image_bytes, provider = _generate_image(image_prompt, negative_prompt, size, image_api_key)

    # 3b. Composite headline + body copy using brand fonts and colour
    brand_color = _extract_brand_color(brand_context)
    image_bytes = overlay_creative(
        image_bytes, headline, sb,
        body_text=caption,
        brand_color=brand_color,
        anthropic=anthropic,
        platform=platform,
    )

    # 4. Upload to storage
    bucket, storage_path, public_url = _upload_to_storage(sb, image_bytes, concept_id)

    print(f"[image_agent] uploaded to {bucket}/{storage_path}", flush=True)

    # 5. Create asset row
    filename = storage_path.split("/")[-1]
    asset_row: dict = {
        "concept_id": concept_id,
        "filename": filename,
        "storage_path": storage_path,
        "mime_type": "image/png",
        "asset_type": "image",
        "generator": "artcaffe-ai",
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

    return {**saved, "_prompt": image_prompt, "_provider": provider, "_size": size, "_source": "remix" if source_url else "generated"}


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


# ---------------------------------------------------------------------------
# Apply overlay to an existing (selected) asset
# ---------------------------------------------------------------------------
def apply_overlay_to_asset(
    *,
    sb: Client,
    anthropic: Any = None,
    asset: dict,
    headline: str,
    concept_id: str,
    content_item_id: str | None,
) -> dict:
    """
    Download an existing asset image, burn the headline onto it, re-upload
    as a new asset row and attach it to content_item_id.
    Returns the new asset dict, or the original asset if anything fails.
    """
    import httpx  # noqa: PLC0415

    public_url = asset.get("public_url", "")
    if not public_url:
        return asset

    try:
        r = httpx.get(public_url, timeout=30.0)
        r.raise_for_status()
        composited = overlay_creative(r.content, headline, sb, anthropic=anthropic)

        bucket, storage_path, new_url = _upload_to_storage(sb, composited, concept_id)
        filename = storage_path.split("/")[-1]

        new_asset: dict = {
            "concept_id": concept_id,
            "filename": filename,
            "storage_path": storage_path,
            "mime_type": "image/png",
            "asset_type": "image",
            "generator": "artcaffe-ai",
            "platform": asset.get("platform", "instagram"),
            "public_url": new_url,
            "created_at": _now(),
        }
        insert_res = sb.table("assets").insert(new_asset).execute()
        saved = insert_res.data[0] if insert_res.data else new_asset

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

        print(f"[image_agent] overlay applied to selected asset → {new_url}", flush=True)
        return {**saved, "_mode": "selected_with_overlay", "_original_asset_id": asset.get("id")}

    except Exception as exc:
        print(f"[image_agent] overlay on selected asset failed, returning original: {exc}", flush=True)
        return asset

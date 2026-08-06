"""
image_agent.py
--------------
Generates marketing banner images using an AI image generation API.

Workflow (preferred — ideation asset present):
  1. Fetch the existing image attached to the content item during ideation
  2. Claude generates an enhancement prompt describing the subject
  3. Provider (Ideogram or OpenAI) enhances the photo while preserving the subject
  4. Text overlay (headline + body) composited via image_overlay
  5. Composited image uploaded to Supabase Storage + asset row created

Workflow (fallback — no ideation asset):
  1. Claude generates a brand-aligned text-to-image prompt
  2. Provider creates a fresh banner image
  3–5. Same as above

Env vars:
  IDEOGRAM_API_KEY    — Ideogram V2 API key
  OPENAI_API_KEY      — OpenAI API key (for gpt-image-1)
  ASSETS_BUCKET       — storage bucket for generated images (default: "generated-assets")
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from supabase import Client

from brand_context import get_active
from image_overlay import overlay_creative, overlay_freeform
from brand_assets_context import format_for_image_prompt, load_brand_assets_context

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
IDEOGRAM_API_KEY = os.environ.get("IDEOGRAM_API_KEY", "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
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
- Size: "1080x1350" for Instagram feed (4:5 portrait — DEFAULT), "1024x1024" for square posts, "1792x1024" for landscape banners, "1024x1792" for stories
- Describe a real, photographic scene — not illustration or cartoon style\
"""

REMIX_PROMPT_SYSTEM = """\
You are a food photographer retouching an existing product photo for Artcaffe Coffee & Restaurant.
The source image shows the exact food/product that must appear in the final banner — DO NOT change,
replace, or add any food items. Your job is ONLY to describe photographic enhancements.

Output ONLY a JSON object — no markdown, no prose:
{
  "prompt": "enhancement description",
  "negative_prompt": "what to avoid",
  "size": "1024x1024"
}

Rules:
- Start with "Enhance this exact photo:" — never invent new subjects
- Describe lighting, styling, and background improvements only
- Keep the same food items, same plate/surface, same composition
- Upgrade to: professional studio food photography, soft natural window light,
  shallow depth of field, warm tones, clean neutral background or rustic wood surface
- NO text, logos, or watermarks
- Size: "1024x1024" for square posts, "1792x1024" for landscape, "1024x1792" for stories\
"""

OPENAI_BANNER_SYSTEM = """\
You are an award-winning Creative Director specialising in premium food, retail, and lifestyle advertising.

Your job is to transform a product image into a luxury social media advertisement that looks like it was
designed by a senior agency designer for OpenAI gpt-image-1.

OBJECTIVE
The uploaded product image is ALWAYS the hero of the advertisement.
- Never replace the product.
- Never invent a different product.
- Never crop important parts of the product.
- Preserve branding and packaging.

DESIGN PHILOSOPHY — campaigns from: Apple · Aesop · Whole Foods · Erewhon · Starbucks Reserve · Artcaffé Market
Minimal. Premium. Luxury. Editorial. Magazine-quality. Never clipart or a template.

LAYOUT
- Strong visual hierarchy.
- Generous whitespace.
- Product remains the focal point.
- Text NEVER covers important parts of the product — place in empty background, corners, negative space, margins.

TYPOGRAPHY
- Bold condensed sans-serif: Bebas Neue / Oswald / Anton / DIN Condensed / Helvetica Condensed
- Hierarchy: Headline → Supporting copy → CTA → Brand
- Large type, tight spacing. Never decorative fonts.

COLOUR PALETTE — extract from product; prefer:
Deep Forest Green · Cream · Warm White · Charcoal · Sand · Dark Brown · Muted Gold · Warm Red
Never random saturated colours.

IMAGE TREATMENT
- Preserve the uploaded product exactly.
- Improve: lighting, contrast, clarity, texture, depth.
- Subtle shadows, realistic lighting. Never distort food, never change packaging.

TEXT PLACEMENT
- Headline: upper third, large, maximum impact.
- Supporting copy: below headline, smaller, readable.
- CTA: bottom third, large pill button, high contrast.
- Logo: top corner, small, clean.
- Footer: website · brand · optional slogan.

DECORATIVE ELEMENTS ALLOWED: thin lines · soft gradients · subtle leaf motifs · minimal icons · soft shadows · small badges
NOT ALLOWED: confetti · stickers · comic graphics · busy backgrounds · cheap effects

FOOD ADVERTISING RULES
Food must always look: fresh · warm · appetising · premium · crispy · natural · high-end.
Lighting should resemble professional food photography.

Output ONLY a JSON object — no markdown, no prose:
{
  "prompt": "...",
  "negative_prompt": "...",
  "size": "1080x1350"
}
"""


FULL_BANNER_SYSTEM = """\
You are a senior art director writing prompts for Ideogram V2
to produce marketing banners for Artcaffe Coffee & Restaurant — a premium café brand in Nairobi, Kenya.

CRITICAL RULE when a source photo is supplied:
  - DO NOT describe, mention, or reference the food or product in the photo.
  - DO NOT alter, reimagine, or regenerate the food or product.
  - Describing the food causes the AI to replace it with a different dish — never do this.
  - Your job is only to ADD text and a dark overlay ON TOP of the existing photo.
  - Never introduce new food, new props, or a new scene.

Output ONLY a JSON object — no markdown, no prose:
{
  "prompt": "...",
  "negative_prompt": "...",
  "size": "1080x1350"
}

PROMPT FORMAT that works well with Ideogram:

  "Source photo used as full-bleed background — do not alter or replace any food or objects.
   Semi-transparent dark overlay ([X]% opacity) across the entire image for text legibility.
   Large bold white sans-serif text '[HEADLINE]' centred in the upper half of the image.
   Smaller white text '[CAPTION]' centred near the bottom.
   [Optional: thin white horizontal rule separating headline from caption.]
   [Optional: small pill badge '[BADGE TEXT]' top-right corner.]
   Clean, minimal layout. No clutter."

NEVER describe the food, dish, or any objects in the image.
Describing food will cause Ideogram to regenerate a different dish — do NOT do this.

Artcaffe brand voice: premium, warm, aspirational — Nairobi urban lifestyle.
Colours if generating from scratch (no source photo): forest green #1B3A2A, cream #F5EBD5.

SIZE: "1080x1350" (Instagram 4:5, DEFAULT) · "1024x1792" (Stories) · "1024x1024" (Square)

Negative prompt should always include:
  "altered food, regenerated dish, different meal, cartoon, illustration, watermark, blurry text,
   illegible text, decorative fonts, script fonts, multiple conflicting styles"

Style variants:
  editorial  → light/cream dark overlay (30%), headline top-center, clean sans-serif
  lifestyle  → warm dark overlay (45%), headline center, slightly more atmospheric
  bold       → deep dark overlay (60%), oversized headline dominates, high contrast
"""


def _make_remix_prompt(
    anthropic: Any,
    *,
    brand_context: dict,
    headline: str,
    caption: str,
    platform: str,
    brand_assets_ctx: str = "",
) -> dict:
    """Ask Claude to write an enhancement-only prompt (does not change the food subject)."""
    parts = [
        "BRAND CONTEXT:\n" + json.dumps(brand_context, indent=2),
        "",
        f"HEADLINE: {headline}",
        f"CAPTION: {caption}",
        f"PLATFORM: {platform}",
    ]
    if brand_assets_ctx:
        parts += ["", brand_assets_ctx]
    parts += ["", "Write a photographic enhancement prompt. Preserve the exact food subject in the source image."]
    user_msg = "\n".join(parts)
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=400,
        system=REMIX_PROMPT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        timeout=20.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(raw)


_PLATFORM_SPEC = {
    "instagram": "Instagram Feed 1080×1350",
    "story":     "Instagram Story 1080×1920",
    "facebook":  "Facebook Feed 1200×1500",
    "google_ads":"Google Display — responsive",
}


def _make_full_banner_prompt(
    anthropic: Any,
    *,
    brand_context: dict,
    headline: str,
    caption: str,
    platform: str,
    brand_assets_ctx: str = "",
    style_variant: str = "editorial",
    provider: str = "ideogram",
    custom_system_prompt: str = "",
) -> dict:
    """
    Ask Claude to write a banner prompt for the given provider.
    provider: "openai" uses the creative-director system prompt with dynamic template injection.
              "ideogram" uses the overlay-only system prompt.
    style_variant: "editorial" | "lifestyle" | "bold"
    """
    brand_name  = brand_context.get("name") or brand_context.get("brand_name") or "Artcaffé Market"
    website     = brand_context.get("website") or "www.artcaffe.co.ke"
    audience    = brand_context.get("target_audience") or "Premium food and coffee lovers in Nairobi, Kenya"
    colors_raw  = brand_context.get("colors") or brand_context.get("brand_colors") or {}
    if isinstance(colors_raw, dict):
        colors_str = ", ".join(f"{k}: {v}" for k, v in list(colors_raw.items())[:4])
    elif isinstance(colors_raw, list):
        colors_str = ", ".join(str(c) for c in colors_raw[:4])
    else:
        colors_str = str(colors_raw) or "Forest Green #1B3A2A, Cream #F5EBD5, Warm Red #C0392B"
    platform_spec = _PLATFORM_SPEC.get(platform, "Instagram Feed 1080×1350")

    if provider == "openai":
        system = custom_system_prompt.strip() or OPENAI_BANNER_SYSTEM
        user_msg = f"""\
Create a premium editorial-style social media banner.

Brand: {brand_name}
Product: (use the uploaded product image — do not replace or invent a new product)
Headline: {headline}
Subheadline: {caption}
CTA: Order Now
Logo: {brand_name} (top corner, small, clean)
Website: {website}
Primary Colors: {colors_str}
Target Audience: {audience}
Platform: {platform_spec}
Style Variant: {style_variant}

Style:
Luxury, minimal, editorial, magazine-quality, premium food advertising, strong typography,
clean grid layout, generous whitespace, subtle gradients, realistic lighting, high-end retail branding.

Rules:
- Preserve the uploaded product exactly — never replace, alter, or crop it.
- Never cover the product with text — use only available negative space.
- Keep the product as the visual hero.
- Improve lighting and shadows naturally.
- Place the logo elegantly in a corner.
- Produce a campaign-quality advertisement suitable for Meta Ads, Google Ads, and Instagram.

Output ONLY a JSON object: {{"prompt":"...","negative_prompt":"...","size":"1080x1350"}}"""
    else:
        # ── Ideogram: overlay-only, never describe the food ──────────────────
        system = FULL_BANNER_SYSTEM
        parts = [
            "BRAND CONTEXT:\n" + json.dumps(brand_context, indent=2),
            "",
            f'HEADLINE (quote verbatim in the poster): "{headline}"',
            f'CAPTION: "{caption}"',
            f"PLATFORM: {platform}",
            f"STYLE VARIANT: {style_variant}",
        ]
        if brand_assets_ctx:
            parts += ["", brand_assets_ctx]
        parts += [
            "",
            f"Write the Ideogram prompt in the '{style_variant}' style. "
            "CRITICAL: do NOT describe or mention the food, dish, or any objects in the source photo — "
            "describing food causes Ideogram to replace it with a completely different dish. "
            "Start with: 'Source photo used as full-bleed background — do not alter or replace any food or objects.' "
            "Then add only: the overlay opacity for this style, the headline verbatim in large bold white sans-serif, "
            "the caption in smaller white text, and optionally a badge. Keep it clean and minimal.",
        ]
        user_msg = "\n".join(p for p in parts if p)

    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        timeout=25.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(raw)


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
    brand_assets_ctx: str = "",
) -> dict:
    """Ask Claude to write a detailed image generation prompt."""
    parts = [
        "BRAND CONTEXT:\n" + json.dumps(brand_context, indent=2),
        "",
        f"HEADLINE: {headline}",
        f"CAPTION: {caption}",
        f"PLATFORM: {platform}",
    ]
    if brand_assets_ctx:
        parts += ["", brand_assets_ctx]
    parts += ["", "Write the image prompt for a marketing banner that fits this content."]
    user_msg = "\n".join(parts)
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

_ASPECT_MAP = {
    "1024x1024": "ASPECT_1_1",
    "1792x1024": "ASPECT_16_9",
    "1024x1792": "ASPECT_9_16",
    "1080x1350": "ASPECT_3_4",   # Instagram 4:5 — closest valid Ideogram enum (ASPECT_4_5 not supported)
}


def _resolve_api_key(key_override: str) -> str:
    key = key_override.strip() or IDEOGRAM_API_KEY
    if not key:
        raise RuntimeError(
            "No image provider configured. Add your Ideogram API key in Settings → AI image generation."
        )
    return key


def _resolve_openai_key(key_override: str) -> str:
    key = key_override.strip() or OPENAI_API_KEY
    if not key:
        raise RuntimeError(
            "No OpenAI key configured. Add your OpenAI API key in Settings → AI image generation."
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
    if not r.is_success:
        raise RuntimeError(f"Ideogram /generate {r.status_code}: {r.text[:400]}")
    return _download_image_url(r.json()["data"][0]["url"])


def _remix_ideogram(
    source_bytes: bytes,
    prompt: str,
    negative_prompt: str,
    size: str,
    api_key: str,
    image_weight: int = 90,
) -> bytes:
    """
    Image-to-image remix via Ideogram V2 /remix.
    image_weight: 0–100, higher = closer to the original photo (90 = preserve product, add text only).
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
    # Convert source to JPEG — Ideogram remix requires JPEG or PNG; mislabelled images cause 400s
    from PIL import Image as _PIL_Image
    _img = _PIL_Image.open(_io.BytesIO(source_bytes)).convert("RGB")
    _buf = _io.BytesIO()
    _img.save(_buf, format="JPEG", quality=92)
    jpeg_bytes = _buf.getvalue()

    r = httpx.post(
        "https://api.ideogram.ai/remix",
        headers={"Api-Key": api_key},
        data={"image_request": image_request},
        files={"image_file": ("source.jpg", _io.BytesIO(jpeg_bytes), "image/jpeg")},
        timeout=120.0,
    )
    if not r.is_success:
        raise RuntimeError(f"Ideogram /remix {r.status_code}: {r.text[:400]}")
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
# OpenAI image generation (gpt-image-1)
# ---------------------------------------------------------------------------

_OPENAI_SIZE_MAP = {
    "1024x1024": "1024x1024",
    "1792x1024": "1536x1024",   # closest landscape
    "1024x1792": "1024x1536",   # closest portrait
    "1080x1350": "1024x1536",   # 4:5 Instagram portrait
}


def _generate_openai(prompt: str, size: str, api_key: str) -> bytes:
    """Text-to-image via OpenAI gpt-image-1. Returns PNG bytes."""
    import base64 as _b64
    import openai as _oai
    client = _oai.OpenAI(api_key=api_key, timeout=90.0)
    oai_size = _OPENAI_SIZE_MAP.get(size, "1024x1024")
    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        n=1,
        size=oai_size,
    )
    return _b64.b64decode(response.data[0].b64_json)


def _edit_openai(source_bytes: bytes, prompt: str, size: str, api_key: str) -> bytes:
    """Image-to-image edit via OpenAI gpt-image-1. Returns PNG bytes."""
    import base64 as _b64
    import io as _io
    import openai as _oai
    client = _oai.OpenAI(api_key=api_key, timeout=90.0)
    oai_size = _OPENAI_SIZE_MAP.get(size, "1024x1024")
    response = client.images.edit(
        model="gpt-image-1",
        image=("source.png", _io.BytesIO(source_bytes), "image/png"),
        prompt=prompt,
        n=1,
        size=oai_size,
    )
    return _b64.b64decode(response.data[0].b64_json)


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


def _bucket_and_path_from_url(public_url: str) -> tuple[str, str]:
    """Recover (bucket, storage_path) from a Supabase Storage public URL."""
    marker = "/object/public/"
    idx = public_url.find(marker)
    if idx < 0:
        raise RuntimeError(f"Not a Supabase Storage public URL: {public_url}")
    rest = public_url[idx + len(marker):]
    bucket, _, path = rest.partition("/")
    if not path:
        raise RuntimeError(f"Could not parse storage path from URL: {public_url}")
    return bucket, path


def _upload_in_place(sb: Client, bucket: str, storage_path: str, content_bytes: bytes, content_type: str) -> str:
    """Overwrite an existing storage object at the same path — its public URL never changes."""
    sb.storage.from_(bucket).upload(
        storage_path,
        content_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return sb.storage.from_(bucket).get_public_url(storage_path)


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
    image_provider: str = "ideogram",
    custom_system_prompt: str = "",
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

    # 1b. Brand assets context (logos + image guidelines extracted from uploaded PDFs)
    assets_ctx = load_brand_assets_context(sb, anthropic)
    brand_assets_text = format_for_image_prompt(assets_ctx)

    # 2. Generate image prompt via Claude
    prompt_data = _make_image_prompt(
        anthropic,
        brand_context=brand_context,
        headline=headline,
        caption=caption,
        platform=platform,
        brand_assets_ctx=brand_assets_text,
    )
    image_prompt = prompt_data.get("prompt", "").strip()
    negative_prompt = prompt_data.get("negative_prompt", "")
    size = prompt_data.get("size", "1024x1024")

    if not image_prompt:
        raise RuntimeError("Claude returned an empty image prompt")

    print(f"[image_agent] prompt='{image_prompt[:120]}…' size={size}", flush=True)

    # 3. Two distinct paths depending on whether an API key is present:
    #
    #   KEY PRESENT — full AI poster (text + layout rendered by provider):
    #     photo + poster prompt → provider edit/remix → complete poster (no PIL overlay)
    #     no photo              → provider text-to-image → complete poster
    #
    #   NO KEY — overlay-only path (PIL templates):
    #     photo → apply overlay_creative() (bar/split/solid)
    #     no photo → raises (caught upstream → select_best_asset fallback)
    source_url   = _get_ideation_asset_url(sb, content_item_id) if content_item_id else None
    resolved_key = (image_api_key or "").strip()
    brand_color  = _extract_brand_color(brand_context)
    overlay_source_bytes: Optional[bytes] = None  # set below only when overlay_creative() actually runs

    if resolved_key:
        if image_provider == "openai":
            # ── OpenAI path: full poster rendered by gpt-image-1 (text + layout) ──
            full_prompt_data = _make_full_banner_prompt(
                anthropic,
                brand_context=brand_context,
                headline=headline,
                caption=caption,
                platform=platform,
                brand_assets_ctx=brand_assets_text,
                style_variant="editorial",
                provider="openai",
                custom_system_prompt=custom_system_prompt,
            )
            fp  = full_prompt_data.get("prompt", image_prompt)
            fsz = full_prompt_data.get("size", size)
            oai_key = _resolve_openai_key(resolved_key)
            if source_url:
                source_bytes = _download_image_url(source_url)
                image_bytes  = _edit_openai(source_bytes, fp, fsz, oai_key)
                provider     = "openai-edit"
            else:
                image_bytes  = _generate_openai(fp, fsz, oai_key)
                provider     = "openai-generate"
            print(f"[image_agent] OpenAI {provider} {len(image_bytes)//1024} KB", flush=True)
            # No PIL overlay — OpenAI rendered the complete poster
        else:
            # ── Ideogram full-creative path: Ideogram renders everything ──────────
            full_prompt_data = _make_full_banner_prompt(
                anthropic,
                brand_context=brand_context,
                headline=headline,
                caption=caption,
                platform=platform,
                brand_assets_ctx=brand_assets_text,
                style_variant="editorial",
            )
            fp  = full_prompt_data.get("prompt", image_prompt)
            fnp = full_prompt_data.get("negative_prompt", negative_prompt)
            fsz = full_prompt_data.get("size", size)
            print(f"[image_agent] IDEOGRAM PROMPT >>>\n{fp}\n<<<", flush=True)
            if source_url:
                source_bytes = _download_image_url(source_url)
                image_bytes  = _remix_ideogram(source_bytes, fp, fnp, fsz, resolved_key)
                provider     = "ideogram-remix"
            else:
                image_bytes, provider = _generate_image(fp, fnp, fsz, resolved_key)
            print(f"[image_agent] Ideogram {provider} {len(image_bytes)//1024} KB", flush=True)
    else:
        # ── Overlay-only path — PIL templates applied to existing photo ─────────
        if not source_url:
            raise RuntimeError(
                "No source image found and AI generation is disabled. "
                "Upload a product photo to the content item or enable AI image generation in Settings."
            )
        source_bytes = _download_image_url(source_url)
        overlay_source_bytes = source_bytes
        image_bytes  = overlay_creative(
            source_bytes, headline, sb,
            body_text=caption,
            brand_color=brand_color,
            anthropic=anthropic,
            platform=platform,
        )
        provider = "ideation-asset"
        print(f"[image_agent] overlay applied to ideation asset {len(image_bytes)//1024} KB", flush=True)

    # 4. Upload to storage
    bucket, storage_path, public_url = _upload_to_storage(sb, image_bytes, concept_id)

    print(f"[image_agent] uploaded to {bucket}/{storage_path}", flush=True)

    # 4b. Persist the clean pre-overlay source too, so the headline text can be
    # re-composited later without re-baking on top of already-burned-in text.
    metadata: dict = {}
    if overlay_source_bytes is not None:
        _, _, overlay_source_url = _upload_to_storage(sb, overlay_source_bytes, concept_id)
        metadata = {"overlay_source_url": overlay_source_url, "overlay_headline": headline}

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
        "metadata": metadata,
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

    ideogram_prompt = fp if resolved_key and image_provider != "openai" else None
    return {**saved, "_prompt": image_prompt, "_ideogram_prompt": ideogram_prompt, "_provider": provider, "_size": size, "_source": "remix" if source_url else "generated"}


# ---------------------------------------------------------------------------
# Multi-variant generation — 3 templates from the same source image
# ---------------------------------------------------------------------------

_VARIANT_TEMPLATES  = ["bar", "split", "solid"]
_IDEOGRAM_STYLES    = ["editorial", "lifestyle", "bold"]


def run_banner_variants(
    *,
    sb: Client,
    anthropic: Any,
    concept_id: str,
    content_item_id: str | None,
    headline: str,
    caption: str,
    platform: str = "instagram",
    image_api_key: str = "",
    image_provider: str = "ideogram",
    custom_system_prompt: str = "",
) -> list[dict]:
    """
    Generate 3 banner variants (bar / split / solid) from the same source image.
    The source image is fetched/generated only once, then 3 different overlays are applied.
    All variants are uploaded and attached to content_item_id.
    Returns a list of asset dicts (up to 3).
    """
    # 1. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(f"No active brand context for concept_id={concept_id}")
    brand_context = ctx.get("context_json") or ctx

    # 1b. Brand assets context
    assets_ctx = load_brand_assets_context(sb, anthropic)
    brand_assets_text = format_for_image_prompt(assets_ctx)

    # 2. Generate image prompt
    prompt_data = _make_image_prompt(
        anthropic,
        brand_context=brand_context,
        headline=headline,
        caption=caption,
        platform=platform,
        brand_assets_ctx=brand_assets_text,
    )
    image_prompt = prompt_data.get("prompt", "").strip()
    negative_prompt = prompt_data.get("negative_prompt", "")
    size = prompt_data.get("size", "1024x1024")

    if not image_prompt:
        raise RuntimeError("Claude returned an empty image prompt")

    source_url   = _get_ideation_asset_url(sb, content_item_id) if content_item_id else None
    resolved_key = (image_api_key or "").strip()
    brand_color  = _extract_brand_color(brand_context)

    # ── Determine which variant loop to run ────────────────────────────────────
    #
    #   KEY PRESENT — 3 Ideogram style variants (editorial / lifestyle / bold)
    #     Each calls Ideogram with a different style prompt; text rendered by AI.
    #     Photo is used as remix source if present, otherwise text-to-image.
    #
    #   NO KEY — 3 PIL template overlays (bar / split / solid) on existing photo.
    #     Requires a product photo to be attached to the content item.

    saved_assets: list[dict] = []

    if resolved_key:
        if image_provider == "openai":
            # ── OpenAI: 3 style variants, gpt-image-1 renders complete poster ─────
            oai_key = _resolve_openai_key(resolved_key)
            raw_source: bytes | None = _download_image_url(source_url) if source_url else None

            _first_ai_error: Exception | None = None
            for style in _IDEOGRAM_STYLES:
                try:
                    full_prompt_data = _make_full_banner_prompt(
                        anthropic,
                        brand_context=brand_context,
                        headline=headline,
                        caption=caption,
                        platform=platform,
                        brand_assets_ctx=brand_assets_text,
                        style_variant=style,
                        provider="openai",
                        custom_system_prompt=custom_system_prompt,
                    )
                    fp  = full_prompt_data.get("prompt", image_prompt)
                    fsz = full_prompt_data.get("size", size)
                    if raw_source:
                        variant_bytes = _edit_openai(raw_source, fp, fsz, oai_key)
                        prov = f"openai-edit-{style}"
                    else:
                        variant_bytes = _generate_openai(fp, fsz, oai_key)
                        prov = f"openai-generate-{style}"
                    print(f"[image_agent] OpenAI variant {style} {len(variant_bytes)//1024} KB", flush=True)
                    bucket, storage_path, public_url = _upload_to_storage(sb, variant_bytes, concept_id)
                    filename = storage_path.split("/")[-1]
                    asset_row: dict = {
                        "concept_id":   concept_id,
                        "filename":     filename,
                        "storage_path": storage_path,
                        "mime_type":    "image/png",
                        "asset_type":   "image",
                        "generator":    "artcaffe-ai",
                        "platform":     platform,
                        "public_url":   public_url,
                        "created_at":   _now(),
                    }
                    insert_res = sb.table("assets").insert(asset_row).execute()
                    saved = insert_res.data[0] if insert_res.data else asset_row
                    saved_assets.append({**saved, "_style": style, "_provider": prov})
                except Exception as exc:
                    print(f"[image_agent] OpenAI variant {style} failed: {exc}", flush=True)
                    if _first_ai_error is None:
                        _first_ai_error = exc

            if not saved_assets and _first_ai_error is not None:
                raise RuntimeError(f"All OpenAI variants failed: {_first_ai_error}") from _first_ai_error

        else:
            # ── Ideogram full-creative: 3 style variants, Ideogram renders everything
            raw_source_bytes: bytes | None = _download_image_url(source_url) if source_url else None

            _first_ai_error = None
            for style in _IDEOGRAM_STYLES:
                try:
                    full_prompt_data = _make_full_banner_prompt(
                        anthropic,
                        brand_context=brand_context,
                        headline=headline,
                        caption=caption,
                        platform=platform,
                        brand_assets_ctx=brand_assets_text,
                        style_variant=style,
                    )
                    fp  = full_prompt_data.get("prompt", image_prompt)
                    fnp = full_prompt_data.get("negative_prompt", negative_prompt)
                    fsz = full_prompt_data.get("size", size)
                    print(f"[image_agent] IDEOGRAM PROMPT [{style}] >>>\n{fp}\n<<<", flush=True)
                    if raw_source_bytes:
                        variant_bytes = _remix_ideogram(raw_source_bytes, fp, fnp, fsz, resolved_key)
                        prov = f"ideogram-remix-{style}"
                    else:
                        variant_bytes, _ = _generate_image(fp, fnp, fsz, resolved_key)
                        prov = f"ideogram-generate-{style}"
                    print(f"[image_agent] Ideogram variant {style} {len(variant_bytes)//1024} KB", flush=True)
                    bucket, storage_path, public_url = _upload_to_storage(sb, variant_bytes, concept_id)
                    filename = storage_path.split("/")[-1]
                    asset_row = {
                        "concept_id":   concept_id,
                        "filename":     filename,
                        "storage_path": storage_path,
                        "mime_type":    "image/png",
                        "asset_type":   "image",
                        "generator":    "artcaffe-ai",
                        "platform":     platform,
                        "public_url":   public_url,
                        "created_at":   _now(),
                    }
                    insert_res = sb.table("assets").insert(asset_row).execute()
                    saved = insert_res.data[0] if insert_res.data else asset_row
                    saved_assets.append({**saved, "_style": style, "_provider": prov})
                except Exception as exc:
                    print(f"[image_agent] Ideogram variant {style} failed: {exc}", flush=True)
                    if _first_ai_error is None:
                        _first_ai_error = exc

            if not saved_assets and _first_ai_error is not None:
                raise RuntimeError(f"All Ideogram variants failed: {_first_ai_error}") from _first_ai_error

    else:
        # ── Overlay-only path: 3 PIL templates on existing photo ──────────────
        if not source_url:
            raise RuntimeError(
                "No source image found and AI generation is disabled. "
                "Upload a product photo to the content item or enable AI image generation in Settings."
            )
        source_bytes = _download_image_url(source_url)

        _first_overlay_error: Exception | None = None
        for tmpl in _VARIANT_TEMPLATES:
            try:
                variant_bytes = overlay_creative(
                    source_bytes, headline, sb,
                    body_text=caption,
                    brand_color=brand_color,
                    anthropic=anthropic,
                    platform=platform,
                    template_override=tmpl,
                )
                bucket, storage_path, public_url = _upload_to_storage(sb, variant_bytes, concept_id)
                filename = storage_path.split("/")[-1]
                asset_row = {
                    "concept_id":   concept_id,
                    "filename":     filename,
                    "storage_path": storage_path,
                    "mime_type":    "image/png",
                    "asset_type":   "image",
                    "generator":    "artcaffe-ai",
                    "platform":     platform,
                    "public_url":   public_url,
                    "created_at":   _now(),
                }
                insert_res = sb.table("assets").insert(asset_row).execute()
                saved = insert_res.data[0] if insert_res.data else asset_row
                saved_assets.append({**saved, "_template": tmpl, "_provider": "ideation-asset"})
                print(f"[image_agent] overlay variant {tmpl} uploaded", flush=True)
            except Exception as exc:
                print(f"[image_agent] overlay variant {tmpl} failed: {exc}", flush=True)
                if _first_overlay_error is None:
                    _first_overlay_error = exc

        if not saved_assets and _first_overlay_error is not None:
            raise RuntimeError(f"All overlay variants failed: {_first_overlay_error}") from _first_overlay_error

    # 5. Attach all variants to content item in one update
    if content_item_id and saved_assets:
        try:
            item_res = (
                sb.table("content_items")
                .select("asset_ids")
                .eq("id", content_item_id)
                .single()
                .execute()
            )
            if item_res.data:
                existing = item_res.data.get("asset_ids") or []
                new_ids = [a["id"] for a in saved_assets if a.get("id")]
                sb.table("content_items").update({
                    "asset_ids": list({*existing, *new_ids}),
                    "updated_at": _now(),
                }).eq("id", content_item_id).execute()
        except Exception as exc:
            print(f"[image_agent] attach variants to item failed: {exc}", flush=True)

    return saved_assets


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

        # Persist the clean pre-overlay source too, so the headline can be
        # re-composited later without re-baking on top of already-burned-in text.
        _, _, overlay_source_url = _upload_to_storage(sb, r.content, concept_id)

        new_asset: dict = {
            "concept_id": concept_id,
            "filename": filename,
            "storage_path": storage_path,
            "mime_type": "image/png",
            "asset_type": "image",
            "generator": "artcaffe-ai",
            "platform": asset.get("platform", "instagram"),
            "public_url": new_url,
            "metadata": {"overlay_source_url": overlay_source_url, "overlay_headline": headline},
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


# ---------------------------------------------------------------------------
# Edit the headline text already baked onto a content item's image(s)
# ---------------------------------------------------------------------------
def customize_headline_text(
    *,
    sb: Client,
    anthropic: Any = None,
    content_item_id: str,
    headline: str,
) -> dict:
    """
    Update a content item's headline and re-composite it onto every attached
    image asset that has a clean pre-overlay source (metadata.overlay_source_url).
    Assets without one (e.g. full-AI-rendered posters) are reported as skipped —
    those need a full regenerate instead, since there's no clean photo to re-composite.
    """
    item_res = (
        sb.table("content_items")
        .select("id,asset_ids")
        .eq("id", content_item_id)
        .single()
        .execute()
    )
    if not item_res.data:
        raise RuntimeError(f"Content item not found: {content_item_id}")
    item = item_res.data
    asset_ids = item.get("asset_ids") or []

    sb.table("content_items").update({"headline": headline, "updated_at": _now()}).eq("id", content_item_id).execute()

    if not asset_ids:
        return {"ok": True, "updated_assets": [], "skipped_assets": []}

    assets_res = (
        sb.table("assets")
        .select("id,asset_type,metadata,platform,public_url")
        .in_("id", asset_ids)
        .eq("asset_type", "image")
        .execute()
    )
    image_assets = assets_res.data or []

    updated_assets: list[dict] = []
    skipped_assets: list[str] = []

    for old_asset in image_assets:
        overlay_source_url = (old_asset.get("metadata") or {}).get("overlay_source_url")
        if not overlay_source_url:
            skipped_assets.append(old_asset["id"])
            continue

        try:
            r = httpx.get(overlay_source_url, timeout=30.0)
            r.raise_for_status()
            composited = overlay_creative(r.content, headline, sb, anthropic=anthropic)

            # Overwrite the asset's existing storage object in place — same
            # bucket/path means its public_url never changes.
            bucket, storage_path = _bucket_and_path_from_url(old_asset["public_url"])
            _upload_in_place(sb, bucket, storage_path, composited, "image/png")

            new_metadata = {"overlay_source_url": overlay_source_url, "overlay_headline": headline}
            sb.table("assets").update({
                "metadata": new_metadata,
                "updated_at": _now(),
            }).eq("id", old_asset["id"]).execute()

            updated_assets.append({**old_asset, "metadata": new_metadata})
        except Exception as exc:
            print(f"[image_agent] customize_headline_text failed for asset {old_asset['id']}: {exc}", flush=True)
            skipped_assets.append(old_asset["id"])

    return {"ok": True, "updated_assets": updated_assets, "skipped_assets": skipped_assets}


# ---------------------------------------------------------------------------
# Assets-page canvas editor — drag-to-position headline/body/scrim
# ---------------------------------------------------------------------------
def customize_asset_layout(
    *,
    sb: Client,
    asset_id: str,
    layout: dict,
    clean_source_override_url: str | None = None,
) -> dict:
    """
    Re-composite a single asset's headline/body text at explicit, user-chosen
    positions (from the Assets page's drag-to-position layout editor).

    `layout` keys: headline_text, headline_x_pct, headline_y_pct,
    headline_size_pct, headline_color, headline_align, body_text, body_x_pct,
    body_y_pct, body_size_pct, body_color, body_align, scrim_position,
    scrim_height_pct, scrim_opacity.

    `clean_source_override_url`: pass the URL from a pending
    preview_standardize_product_image() call to commit that standardized
    (but not-yet-saved) image as the new clean source in this same Save —
    lets "standardize then add text" happen in one atomic commit instead of
    the standardize taking effect the moment its button is clicked.

    Every image asset is editable: if it has no clean pre-overlay source yet
    (metadata.overlay_source_url — e.g. a catalog photo or a full-AI-rendered
    poster that never went through the PIL overlay pipeline), its CURRENT
    image is snapshotted once as that clean source before the first edit, so
    later edits recomposite onto the untouched original instead of stacking
    text on top of previously-baked text. Overwrites the asset's existing
    storage object in place, so its public_url never changes.
    """
    asset_res = (
        sb.table("assets")
        .select("id,metadata,public_url,asset_type,concept_id")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    if not asset_res.data:
        raise RuntimeError(f"Asset not found: {asset_id}")
    asset = asset_res.data
    if asset.get("asset_type") != "image":
        raise RuntimeError("Only image assets can be edited with the layout editor")

    metadata = asset.get("metadata") or {}
    overlay_source_url = clean_source_override_url or metadata.get("overlay_source_url")
    if not overlay_source_url:
        current_resp = httpx.get(asset["public_url"], timeout=30.0, follow_redirects=True)
        current_resp.raise_for_status()
        current_bytes = _downscale_if_huge(current_resp.content)
        _, _, overlay_source_url = _upload_to_storage(sb, current_bytes, asset["concept_id"])

    r = httpx.get(overlay_source_url, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    source_bytes = _downscale_if_huge(r.content)

    composited = overlay_freeform(
        source_bytes, sb,
        headline_text=layout.get("headline_text", ""),
        headline_x_pct=layout.get("headline_x_pct", 0.07),
        headline_y_pct=layout.get("headline_y_pct", 0.70),
        headline_size_pct=layout.get("headline_size_pct", 0.072),
        headline_color=layout.get("headline_color", "#FFFFFF"),
        headline_align=layout.get("headline_align", "left"),
        headline_font=layout.get("headline_font", ""),
        body_text=layout.get("body_text", ""),
        body_x_pct=layout.get("body_x_pct", 0.07),
        body_y_pct=layout.get("body_y_pct", 0.85),
        body_size_pct=layout.get("body_size_pct", 0.028),
        body_color=layout.get("body_color", "#FFFFFF"),
        body_align=layout.get("body_align", "left"),
        body_font=layout.get("body_font", ""),
        scrim_position=layout.get("scrim_position", "bottom"),
        scrim_height_pct=layout.get("scrim_height_pct", 0.35),
        scrim_opacity=layout.get("scrim_opacity", 0.65),
    )

    # Overwrite the asset's existing storage object in place — same
    # bucket/path means its public_url never changes.
    bucket, storage_path = _bucket_and_path_from_url(asset["public_url"])
    _upload_in_place(sb, bucket, storage_path, composited, "image/png")

    new_metadata = {**metadata, **layout, "overlay_source_url": overlay_source_url}
    sb.table("assets").update({
        "metadata": new_metadata,
        "updated_at": _now(),
    }).eq("id", asset_id).execute()

    return {"ok": True, "asset": {**asset, "metadata": new_metadata}}


# ---------------------------------------------------------------------------
# Product photo standardization — 1000x1000, centered, white background
# ---------------------------------------------------------------------------
_PRODUCT_SHOT_PROMPT = (
    "Professional e-commerce product photography. Isolate the product from this "
    "image and place it centered on a pure white (#FFFFFF) background. No "
    "shadows, no props, no other objects, no text or watermarks. Square 1:1 "
    "composition, product filling most of the frame with a small even margin."
)


_MAX_SOURCE_BYTES = 15 * 1024 * 1024  # 15 MB — reject pathologically large downloads outright
_MAX_SOURCE_DIM = 2000  # downscale before any heavy processing to bound CPU/memory on a small VM


def _downscale_if_huge(image_bytes: bytes, max_dim: int = _MAX_SOURCE_DIM) -> bytes:
    """Cap an image's longest side before feeding it to PIL/OpenAI — a handful
    of concurrent full-resolution decodes can spike CPU/memory enough to
    starve other services (e.g. Postgres) sharing the same small VM."""
    import io as _io
    from PIL import Image
    img = Image.open(_io.BytesIO(image_bytes))
    if max(img.size) <= max_dim:
        return image_bytes
    img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _pad_to_square_white(image_bytes: bytes, size: int = 1000, padding_pct: float = 0.05) -> bytes:
    """Fit an image so its LONGEST side occupies exactly (1 - 2*padding_pct)
    of the canvas, then center it on a pure white size x size square. This
    guarantees a consistent 5% margin on whichever pair of sides is longest —
    top/bottom for a portrait-oriented object, left/right for landscape —
    with the shorter axis centered (and proportionally more padded, since
    aspect ratio is preserved). Output dimensions are always exact regardless
    of the source's aspect ratio or the AI provider's actual output size."""
    import io as _io
    from PIL import Image
    img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
    target = max(1, int(size * (1 - 2 * padding_pct)))
    img.thumbnail((target, target), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    x = (size - img.width) // 2
    y = (size - img.height) // 2
    canvas.paste(img, (x, y))
    buf = _io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _run_standardize_transform(source_bytes: bytes, mode: str, openai_api_key: str) -> bytes:
    """Shared by standardize_product_image() and its preview variant."""
    source_bytes = _downscale_if_huge(source_bytes)
    if mode == "openai":
        oai_key = _resolve_openai_key(openai_api_key)
        edited_bytes = _edit_openai(source_bytes, _PRODUCT_SHOT_PROMPT, "1024x1024", oai_key)
        return _pad_to_square_white(edited_bytes, 1000)
    return _pad_to_square_white(source_bytes, 1000)


def preview_standardize_product_image(
    *,
    sb: Client,
    asset_id: str,
    mode: str = "simple",
    openai_api_key: str = "",
) -> dict:
    """
    Run the same standardize transform as standardize_product_image(), but
    write the result to a NEW temporary storage object instead of the asset's
    own path — nothing is committed. The layout editor shows this as a live
    preview and only persists it if/when the user clicks Save (which passes
    this preview_url back as clean_source_override_url to
    customize_asset_layout()).
    """
    asset_res = (
        sb.table("assets")
        .select("id,public_url,asset_type,concept_id")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    if not asset_res.data:
        raise RuntimeError(f"Asset not found: {asset_id}")
    asset = asset_res.data
    if asset.get("asset_type") != "image":
        raise RuntimeError("Only image assets can be standardized")

    resp = httpx.get(asset["public_url"], timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    source_bytes = resp.content
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise RuntimeError(
            f"Source image is too large to standardize ({len(source_bytes) // (1024*1024)} MB, "
            f"max {_MAX_SOURCE_BYTES // (1024*1024)} MB)"
        )
    final_bytes = _run_standardize_transform(source_bytes, mode, openai_api_key)
    _, _, preview_url = _upload_to_storage(sb, final_bytes, asset["concept_id"])
    return {"ok": True, "preview_url": preview_url}


def standardize_product_image(
    *,
    sb: Client,
    asset_id: str,
    mode: str = "simple",
    openai_api_key: str = "",
) -> dict:
    """
    Reformat a single image asset into a standard 1000x1000 product shot —
    centered on a pure white background.

    mode="simple": pure resize + white-pad, no background removal — works
      well for photos already on a plain/white background.
    mode="openai": runs the photo through OpenAI gpt-image-1's image edit
      first to actually remove/replace the background, then pads the result
      to exact 1000x1000 — handles busy/dark backgrounds properly.

    Overwrites the asset's existing storage object in place (same public_url).
    Clears metadata.overlay_source_url so the layout editor re-snapshots this
    new standardized photo as its clean source on the next text edit.
    """
    asset_res = (
        sb.table("assets")
        .select("id,metadata,public_url,asset_type")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    if not asset_res.data:
        raise RuntimeError(f"Asset not found: {asset_id}")
    asset = asset_res.data
    if asset.get("asset_type") != "image":
        raise RuntimeError("Only image assets can be standardized")

    resp = httpx.get(asset["public_url"], timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    source_bytes = resp.content
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise RuntimeError(
            f"Source image is too large to standardize ({len(source_bytes) // (1024*1024)} MB, "
            f"max {_MAX_SOURCE_BYTES // (1024*1024)} MB)"
        )
    final_bytes = _run_standardize_transform(source_bytes, mode, openai_api_key)

    bucket, storage_path = _bucket_and_path_from_url(asset["public_url"])
    _upload_in_place(sb, bucket, storage_path, final_bytes, "image/png")

    metadata = asset.get("metadata") or {}
    new_metadata = {**metadata, "standardized_mode": mode}
    new_metadata.pop("overlay_source_url", None)
    sb.table("assets").update({
        "metadata": new_metadata,
        "updated_at": _now(),
    }).eq("id", asset_id).execute()

    return {"ok": True, "asset": {**asset, "metadata": new_metadata}}


_IMAGE_FORMAT_BY_EXT = {
    "png": ("PNG", "image/png"),
    "jpg": ("JPEG", "image/jpeg"),
    "jpeg": ("JPEG", "image/jpeg"),
    "webp": ("WEBP", "image/webp"),
}


def replace_asset_image(*, sb: Client, asset_id: str, new_image_bytes: bytes) -> dict:
    """
    Swap an image asset's pixel content for a new upload while keeping its
    public_url, storage path, name, and brand tags exactly as they are.

    The new image is re-encoded to match the EXISTING asset's file extension
    (png/jpg/webp) so the URL's format claim stays accurate regardless of what
    format the replacement file was in. Clears metadata.overlay_source_url —
    any previously saved layout state referred to the old pixels and no
    longer applies.
    """
    import io as _io
    from PIL import Image

    asset_res = (
        sb.table("assets")
        .select("id,metadata,public_url,asset_type")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    if not asset_res.data:
        raise RuntimeError(f"Asset not found: {asset_id}")
    asset = asset_res.data
    if asset.get("asset_type") != "image":
        raise RuntimeError("Only image assets can be replaced with this action")

    if len(new_image_bytes) > _MAX_SOURCE_BYTES:
        raise RuntimeError(
            f"New image is too large ({len(new_image_bytes) // (1024*1024)} MB, "
            f"max {_MAX_SOURCE_BYTES // (1024*1024)} MB)"
        )
    new_image_bytes = _downscale_if_huge(new_image_bytes)

    bucket, storage_path = _bucket_and_path_from_url(asset["public_url"])
    ext = storage_path.rsplit(".", 1)[-1].lower() if "." in storage_path else "png"
    pil_format, content_type = _IMAGE_FORMAT_BY_EXT.get(ext, ("PNG", "image/png"))

    img = Image.open(_io.BytesIO(new_image_bytes))
    if pil_format == "JPEG" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, format=pil_format)
    final_bytes = buf.getvalue()

    _upload_in_place(sb, bucket, storage_path, final_bytes, content_type)

    metadata = asset.get("metadata") or {}
    new_metadata = {**metadata}
    new_metadata.pop("overlay_source_url", None)
    sb.table("assets").update({
        "metadata": new_metadata,
        "mime_type": content_type,
        "file_size_bytes": len(final_bytes),
        "updated_at": _now(),
    }).eq("id", asset_id).execute()

    return {"ok": True, "asset": {**asset, "metadata": new_metadata}}

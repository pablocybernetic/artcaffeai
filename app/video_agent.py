"""
video_agent.py
--------------
Generates short marketing videos from an image using Runway Gen-4 (image-to-video).

Workflow:
  1. Resolve source image — use provided image_url or find best asset for the item
  2. Claude Haiku generates a cinematic motion prompt + picks ratio/duration
  3. Submit image-to-video task to Runway (async)
  4. Poll until SUCCEEDED (max 3 min)
  5. Download video bytes
  6. Upload to Supabase Storage
  7. Create asset row (asset_type="video") + attach to content_item

Env vars:
  RUNWAYML_API_SECRET  — Runway API key (required)
  RUNWAY_MODEL         — "gen4_turbo" (default) | "gen3a_turbo"
  VIDEO_BUCKET         — storage bucket (default: "generated-assets")
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from supabase import Client

from brand_context import get_active
from brand_assets_context import format_for_video_prompt, load_brand_assets_context

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
RUNWAY_API_KEY = os.environ.get("RUNWAYML_API_SECRET", "")
RUNWAY_MODEL = os.environ.get("RUNWAY_MODEL", "gen4_turbo")
RUNWAY_BASE = "https://api.dev.runwayml.com"
VIDEO_BUCKET = os.environ.get("VIDEO_BUCKET", "generated-assets")
MAX_POLL_SECONDS = 300  # 5 minutes max wait

# Platform → video ratio
_RATIO_MAP = {
    "instagram_stories": "720:1280",
    "stories": "720:1280",
    "story": "720:1280",
    "reels": "720:1280",
    "reel": "720:1280",
    "instagram_reels": "720:1280",
    "tiktok": "720:1280",
    "facebook": "1280:720",
    "linkedin": "1280:720",
    "youtube": "1280:720",
    "google_ads": "1280:720",
    "instagram": "1104:832",
    "instagram_organic": "1104:832",
}

# ---------------------------------------------------------------------------
# Motion prompt engineering
# ---------------------------------------------------------------------------
MOTION_SYSTEM = """\
You are a cinematic art director for Artcaffe, a premium café brand in Nairobi, Kenya.
Your task: write a precise, premium motion prompt for Runway Gen-4 image-to-video generation.

The video is generated FROM a still marketing photo — the scene already exists in the image.
You are describing MOTION only: camera moves, atmospheric elements, and physical movement
that would make this specific image come alive.

Use every piece of image metadata provided to make the motion feel native to the scene:
- Match camera movement to the scene type (pull-back for interiors, slow pan for food close-ups)
- Match atmospheric motion to what's physically possible (steam from hot drinks, soft fabric movement, ambient crowd)
- Match mood and lighting to the photography style (editorial → cinematic float, candid → handheld drift)
- Reference specific food/drink items when describing motion (e.g. "steam rising from the flat white")
- Use the color palette and mood to set the energy (warm tones → slow, golden, languid movement)

Rules:
- NO fast cuts, shaky cam, or dramatic zooms — Artcaffe is upscale and calm
- Motion should feel like a luxury brand TVC, not a TikTok ad
- The motion prompt should reference what is actually IN the image, not generic café descriptions
- Keep it 1-2 sentences: one for camera, one for atmospheric/subject motion

Output ONLY a JSON object — no markdown, no prose:
{
  "motion_prompt": "precise motion description referencing actual image content",
  "duration": 5,
  "ratio": "1280:720"
}

Ratio guide:
  - Stories / Reels / TikTok → "720:1280"
  - Feed / Facebook / LinkedIn → "1280:720"
  - Instagram square feed → "1104:832"

Duration: 5 seconds for stories/reels/ads; 10 seconds for LinkedIn/YouTube.\
"""


def _build_image_metadata_section(image_metadata: dict) -> str:
    """Format asset analysis metadata into a structured prompt section."""
    if not image_metadata:
        return ""

    parts: list[str] = ["IMAGE METADATA (use this to make the motion specific to THIS photo):"]

    if desc := image_metadata.get("description"):
        parts.append(f"  Description: {desc}")
    if scene := image_metadata.get("scene_type"):
        parts.append(f"  Scene type: {scene}")
    if mood := image_metadata.get("mood"):
        parts.append(f"  Mood: {mood}")
    if style := image_metadata.get("photography_style"):
        parts.append(f"  Photography style: {style}")
    if food := image_metadata.get("food_items"):
        parts.append(f"  Food/drink items visible: {', '.join(food)}")
    if image_metadata.get("people_present") is not None:
        parts.append(f"  People present: {'yes' if image_metadata['people_present'] else 'no'}")
    if palette := image_metadata.get("color_palette"):
        if isinstance(palette, list):
            parts.append(f"  Color palette: {', '.join(palette)}")
        elif isinstance(palette, str):
            parts.append(f"  Color palette: {palette}")
    if lighting := image_metadata.get("lighting"):
        parts.append(f"  Lighting: {lighting}")
    if tags := image_metadata.get("tags"):
        parts.append(f"  Keywords: {', '.join(tags[:10])}")

    return "\n".join(parts)


def _make_motion_prompt(
    anthropic: Any,
    *,
    headline: str,
    caption: str,
    platform: str,
    brand_context: dict,
    image_metadata: Optional[dict] = None,
    brand_assets_ctx: str = "",
) -> dict:
    parts: list[str] = [
        "BRAND CONTEXT:\n" + json.dumps(brand_context, indent=2),
        "",
        f"HEADLINE: {headline}",
        f"CAPTION: {caption}",
        f"PLATFORM: {platform}",
    ]

    meta_section = _build_image_metadata_section(image_metadata or {})
    if meta_section:
        parts += ["", meta_section]

    if brand_assets_ctx:
        parts += ["", brand_assets_ctx]

    parts += ["", "Write the Runway motion prompt for this marketing video."]
    user_msg = "\n".join(parts)

    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=400,
        system=MOTION_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        timeout=20.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[: raw.rfind("```")]
    return json.loads(raw.strip())


# ---------------------------------------------------------------------------
# Runway API — image-to-video
# ---------------------------------------------------------------------------
def _runway_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }


def _submit_video_task(
    *,
    image_url: str,
    motion_prompt: str,
    ratio: str,
    duration: int,
    model: str,
    api_key: str,
) -> str:
    """Submit an image-to-video task to Runway. Returns task_id."""
    payload = {
        "model": model,
        "promptImage": image_url,
        "promptText": motion_prompt,
        "ratio": ratio,
        "duration": duration,
    }
    r = httpx.post(
        f"{RUNWAY_BASE}/v1/image_to_video",
        headers=_runway_headers(api_key),
        json=payload,
        timeout=30.0,
    )
    if not r.is_success:
        raise RuntimeError(f"Runway submission failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    task_id = data.get("id")
    if not task_id:
        raise RuntimeError(f"Runway response missing task id: {data}")
    return task_id


def _poll_task(task_id: str, api_key: str) -> str:
    """Poll Runway task until SUCCEEDED. Returns video URL."""
    deadline = time.time() + MAX_POLL_SECONDS
    while time.time() < deadline:
        r = httpx.get(
            f"{RUNWAY_BASE}/v1/tasks/{task_id}",
            headers=_runway_headers(api_key),
            timeout=15.0,
        )
        if not r.is_success:
            raise RuntimeError(f"Runway poll error {r.status_code}: {r.text[:200]}")
        data = r.json()
        status = data.get("status", "")
        print(f"[video_agent] task {task_id} status={status}", flush=True)

        if status == "SUCCEEDED":
            output = data.get("output") or []
            if not output:
                raise RuntimeError("Runway task succeeded but output is empty")
            return output[0]

        if status in ("FAILED", "CANCELLED"):
            failure = data.get("failure") or data.get("failureCode") or "unknown"
            raise RuntimeError(f"Runway task {status}: {failure}")

        time.sleep(6)

    raise RuntimeError(f"Runway video generation timed out after {MAX_POLL_SECONDS}s")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _upload_video(sb: Client, video_bytes: bytes, concept_id: str) -> tuple[str, str, str]:
    """Upload video to Supabase Storage. Returns (bucket, path, public_url)."""
    filename = f"video_{uuid.uuid4().hex[:10]}.mp4"
    storage_path = f"videos/{concept_id}/{filename}"

    sb.storage.from_(VIDEO_BUCKET).upload(
        storage_path,
        video_bytes,
        file_options={"content-type": "video/mp4", "upsert": "true"},
    )
    public_url = sb.storage.from_(VIDEO_BUCKET).get_public_url(storage_path)
    return VIDEO_BUCKET, storage_path, public_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Resolve source image from content item
# ---------------------------------------------------------------------------
def _resolve_image_asset(sb: Client, content_item_id: str) -> Optional[dict]:
    """Find the best image asset attached to this content item. Returns full asset row."""
    item_res = (
        sb.table("content_items")
        .select("asset_ids")
        .eq("id", content_item_id)
        .single()
        .execute()
    )
    if not item_res.data:
        return None
    asset_ids = item_res.data.get("asset_ids") or []
    if not asset_ids:
        return None

    asset_res = (
        sb.table("assets")
        .select("id,public_url,asset_type,metadata,analysis_status")
        .in_("id", asset_ids)
        .eq("asset_type", "image")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return asset_res.data[0] if asset_res.data else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_video_generation(
    *,
    sb: Client,
    anthropic: Any,
    concept_id: str,
    content_item_id: Optional[str],
    headline: str,
    caption: str,
    platform: str = "instagram",
    image_url: str = "",
    model_override: str = "",
    runway_api_key: str = "",
) -> dict:
    """
    Generate a short marketing video and save it as an asset.
    Returns the saved asset row.
    """
    # Prefer key passed from frontend Settings; fall back to env var
    resolved_key = runway_api_key.strip() or RUNWAY_API_KEY
    if not resolved_key:
        raise RuntimeError(
            "Runway API key not configured. Add it in Settings → AI video generation."
        )

    # 1. Brand context
    ctx = get_active(sb, concept_id)
    if not ctx:
        raise RuntimeError(f"No active brand context for concept_id={concept_id}")
    brand_context = ctx.get("context_json") or ctx

    # 1b. Brand assets context (video guidelines extracted from uploaded PDFs)
    assets_ctx = load_brand_assets_context(sb, anthropic)
    brand_assets_text = format_for_video_prompt(assets_ctx)

    # 2. Resolve source image + metadata
    source_url = image_url.strip()
    image_metadata: Optional[dict] = None

    if not source_url and content_item_id:
        source_asset = _resolve_image_asset(sb, content_item_id)
        if source_asset:
            source_url = source_asset.get("public_url", "")
            # Use rich analysis metadata if the asset has been analysed
            if source_asset.get("analysis_status") == "done":
                image_metadata = source_asset.get("metadata") or {}
                if image_metadata:
                    print(f"[video_agent] using image metadata: {list(image_metadata.keys())}", flush=True)

    if not source_url:
        raise RuntimeError(
            "No source image provided and no image asset found on this content item. "
            "Generate a banner image first, then generate the video."
        )

    print(f"[video_agent] source image: {source_url[:80]}…", flush=True)

    # 3. Generate motion prompt via Claude — enriched with image metadata + brand guidelines
    prompt_data = _make_motion_prompt(
        anthropic,
        headline=headline,
        caption=caption,
        platform=platform,
        brand_context=brand_context,
        image_metadata=image_metadata,
        brand_assets_ctx=brand_assets_text,
    )
    motion_prompt = prompt_data.get("motion_prompt", "Slow cinematic push, warm ambient café light, gentle steam rising from coffee.")
    duration = int(prompt_data.get("duration", 5))
    # Use platform ratio map as primary, fall back to Claude's suggestion
    ratio = _RATIO_MAP.get(platform.lower(), prompt_data.get("ratio", "1280:720"))
    model = model_override.strip() or RUNWAY_MODEL

    print(f"[video_agent] motion='{motion_prompt[:80]}…' ratio={ratio} duration={duration}s model={model}", flush=True)

    # 4. Submit to Runway + poll
    task_id = _submit_video_task(
        image_url=source_url,
        motion_prompt=motion_prompt,
        ratio=ratio,
        duration=duration,
        model=model,
        api_key=resolved_key,
    )
    print(f"[video_agent] submitted task_id={task_id}", flush=True)

    video_url = _poll_task(task_id, resolved_key)
    print(f"[video_agent] video ready: {video_url[:80]}…", flush=True)

    # 5. Download video
    r = httpx.get(video_url, timeout=120.0)
    r.raise_for_status()
    video_bytes = r.content
    print(f"[video_agent] downloaded {len(video_bytes) // 1024} KB", flush=True)

    # 6. Upload to storage
    bucket, storage_path, public_url = _upload_video(sb, video_bytes, concept_id)
    filename = storage_path.split("/")[-1]

    # 7. Create asset row
    asset_row: dict = {
        "concept_id": concept_id,
        "filename": filename,
        "storage_path": storage_path,
        "mime_type": "video/mp4",
        "asset_type": "video",
        "generator": "runway",
        "platform": platform,
        "public_url": public_url,
        "metadata": {
            "motion_prompt": motion_prompt,
            "runway_task_id": task_id,
            "runway_model": model,
            "source_image_url": source_url,
            "ratio": ratio,
            "duration_seconds": duration,
            "source_image_metadata": image_metadata or {},
        },
        "created_at": _now(),
    }
    insert_res = sb.table("assets").insert(asset_row).execute()
    saved = insert_res.data[0] if insert_res.data else asset_row

    # 8. Attach to content item
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

    return {
        **saved,
        "_motion_prompt": motion_prompt,
        "_runway_model": model,
        "_duration": duration,
        "_ratio": ratio,
    }

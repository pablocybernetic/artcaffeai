"""
agent_routes.py
---------------
FastAPI router for the Artcaffe AI agent endpoints.

Prefix: /agents
Auth:   X-Api-Key header (same pattern as data_routes.py)

Endpoints:
  POST   /agents/ideation             — enqueue an ideation job
  POST   /agents/production           — enqueue a production job (approved items only)
  GET    /agents/items/{brief_id}     — list content items for a brief
  PATCH  /agents/items/{item_id}      — update content item status
  POST   /agents/master               — run Master Agent cycle (scan, recover, analyse)
  GET    /agents/master/status        — latest Master Agent report
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

import os

from job_runner import run_job
from image_agent import (
    run_image_generation, run_banner_variants, select_best_asset, apply_overlay_to_asset,
    customize_headline_text, customize_asset_layout, standardize_product_image,
    _bucket_and_path_from_url, _upload_in_place,
)
from image_analysis_agent import analyze_asset as _analyze_asset
from video_agent import run_video_generation
from audio_mux import mux_audio_into_video

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

VALID_STATUSES = {"approved", "rejected", "draft", "pending_review"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return  # auth disabled in dev
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(
    prefix="/agents",
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ResearchRequest(BaseModel):
    concept_id: str


class IdeationRequest(BaseModel):
    brief_id: str
    concept_id: str
    n: int = 5


class ProductionRequest(BaseModel):
    content_item_id: str
    concept_id: str


class ItemStatusUpdate(BaseModel):
    status: str


class BannerRequest(BaseModel):
    concept_id: str
    content_item_id: str          # attach generated image to this item
    headline: str
    caption: str
    platform: str = "instagram"
    image_api_key: str = ""       # provider API key from frontend Settings — overrides env var
    image_provider: str = "ideogram"  # "ideogram" | "openai"
    generate_variants: bool = False  # when True: generate bar + split + solid variants
    custom_system_prompt: str = ""   # admin-editable override for OPENAI_BANNER_SYSTEM


class AnalyzeAssetRequest(BaseModel):
    asset_id: str


class VideoRequest(BaseModel):
    concept_id: str
    content_item_id: str = ""          # attach video to this item (optional)
    headline: str
    caption: str = ""
    platform: str = "instagram"
    image_url: str = ""                # source image URL; auto-resolved from content_item if blank
    runway_api_key: str = ""           # from frontend Settings — overrides RUNWAYML_API_SECRET env var
    runway_model: str = ""             # override RUNWAY_MODEL env (e.g. "gen3a_turbo")


class CustomizeHeadlineRequest(BaseModel):
    content_item_id: str
    headline: str


class CustomizeAudioRequest(BaseModel):
    content_item_id: str
    audio_asset_id: str


class TextLayerParams(BaseModel):
    text: str = ""
    x_pct: float = 0.07
    y_pct: float = 0.70
    size_pct: float = 0.072
    color: str = "#FFFFFF"
    align: str = "left"   # "left" | "center"


class CustomizeAssetLayoutRequest(BaseModel):
    asset_id: str
    headline: TextLayerParams = TextLayerParams()
    body: TextLayerParams = TextLayerParams(y_pct=0.85, size_pct=0.028)
    scrim_position: str = "bottom"     # "top" | "bottom" | "none"
    scrim_height_pct: float = 0.35
    scrim_opacity: float = 0.65


class StandardizeProductRequest(BaseModel):
    asset_id: str
    mode: str = "simple"           # "simple" | "openai"
    openai_api_key: str = ""       # from frontend Settings — overrides OPENAI_API_KEY env var


class PublishNowRequest(BaseModel):
    brief_id: str
    platforms: list[str]  # e.g. ["instagram", "facebook", "linkedin", "google_ads"]


class SchedulePublishRequest(BaseModel):
    brief_id: str
    platforms: list[str]
    publish_at: str  # ISO 8601 UTC timestamp, e.g. "2026-06-10T09:00:00Z"
    content_item_id: Optional[str] = None
    platform_types: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_job(agent_type: str, concept_id: str, payload: dict[str, Any]) -> str:
    """Insert a new pending job row and return the job_id."""
    job_id = str(uuid.uuid4())
    sb.table("jobs").insert(
        {
            "id": job_id,
            "concept_id": concept_id,
            "agent_type": agent_type,
            "status": "pending",
            "input_payload": payload,
            "created_at": _now(),
            "updated_at": _now(),
        }
    ).execute()
    return job_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/research")
def start_research(req: ResearchRequest, bg: BackgroundTasks):
    """
    Enqueue a market research job for a concept.
    The agent analyses platform data + brand context and surfaces
    3-5 actionable content opportunities stored in research_briefs.
    """
    job_id = _create_job(
        agent_type="market_research",
        concept_id=req.concept_id,
        payload={},
    )
    bg.add_task(run_job, job_id)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/research/{concept_id}")
def list_research_briefs(concept_id: str):
    """Return the 10 most recent research briefs for a concept."""
    res = (
        sb.table("research_briefs")
        .select("id,concept_id,period_start,period_end,summary,opportunities,model,created_at")
        .eq("concept_id", concept_id)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    return {"briefs": res.data or []}


@router.post("/ideation")
def start_ideation(req: IdeationRequest, bg: BackgroundTasks):
    """Enqueue an ideation job and process it in the background."""
    job_id = _create_job(
        agent_type="ideation",
        concept_id=req.concept_id,
        payload={"brief_id": req.brief_id, "n": req.n},
    )
    bg.add_task(run_job, job_id)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.post("/production")
def start_production(req: ProductionRequest, bg: BackgroundTasks):
    """
    Enqueue a production job.

    The content item must have status='approved' before production can start.
    Returns 404 if the item is not found, 400 if it is not approved.
    """
    # Verify item exists and is approved
    item_res = (
        sb.table("content_items")
        .select("id,status")
        .eq("id", req.content_item_id)
        .single()
        .execute()
    )
    if not item_res.data:
        raise HTTPException(
            status_code=404,
            detail=f"Content item not found: {req.content_item_id}",
        )
    item = item_res.data
    if item.get("status") != "approved":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Content item '{req.content_item_id}' has status='{item.get('status')}'. "
                "Only approved items can be sent to production."
            ),
        )

    job_id = _create_job(
        agent_type="production",
        concept_id=req.concept_id,
        payload={"content_item_id": req.content_item_id},
    )
    bg.add_task(run_job, job_id)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/items/{brief_id}")
def list_items(brief_id: str):
    """List all content items for a given brief, ordered by creation time."""
    res = (
        sb.table("content_items")
        .select("*")
        .eq("brief_id", brief_id)
        .order("created_at", desc=False)
        .execute()
    )
    return {"items": res.data or []}


@router.patch("/items/{item_id}")
def update_item_status(item_id: str, body: ItemStatusUpdate, bg: BackgroundTasks):
    """
    Update the status of a content item.

    Allowed statuses: approved | rejected | draft | pending_review

    Side-effect: when the new status is pending_review, all active admins and
    content managers receive an approval-needed email notification.
    """
    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid status '{body.status}'. "
                f"Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            ),
        )

    res = (
        sb.table("content_items")
        .update({"status": body.status, "updated_at": _now()})
        .eq("id", item_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Content item not found: {item_id}")

    item = res.data[0]

    # Fire approval notifications in the background (non-blocking).
    if body.status == "pending_review":
        brief_id = item.get("brief_id", "")
        title = item.get("title") or item.get("headline") or "Untitled"

        def _notify():
            from notification_service import notify_approval_needed_to_team  # noqa: PLC0415
            try:
                notify_approval_needed_to_team(sb, brief_id=brief_id, title=title)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent_routes] approval notification failed: {exc}", flush=True)

        bg.add_task(_notify)

    return {"ok": True, "updated": item}


@router.post("/publish")
def publish_now(req: PublishNowRequest, bg: BackgroundTasks):
    """
    Approve a brief and immediately publish its content to the selected platforms.
    Finds the most recent content_item for the brief, runs publishing in the background.
    """
    # Find the most recent content item for this brief
    item_res = (
        sb.table("content_items")
        .select("id")
        .eq("brief_id", req.brief_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not item_res.data:
        raise HTTPException(404, f"No content items found for brief: {req.brief_id}")
    content_item_id = item_res.data[0]["id"]

    brief_res = sb.table("content_briefs").select("concept_id").eq("id", req.brief_id).single().execute()
    if not brief_res.data:
        raise HTTPException(404, f"Brief not found: {req.brief_id}")
    concept_id: str = brief_res.data["concept_id"]

    job_id = _create_job(
        agent_type="scheduled_publish",
        concept_id=concept_id,
        payload={
            "content_item_id": content_item_id,
            "platforms": req.platforms,
            "brief_id": req.brief_id,
        },
    )
    bg.add_task(run_job, job_id)
    return {"ok": True, "job_id": job_id, "content_item_id": content_item_id, "status": "queued"}


@router.post("/schedule")
def schedule_publish(req: SchedulePublishRequest):
    """
    Approve a brief and schedule its content for publishing at a future time.
    The job_runner poller will fire it once publish_at is reached.
    """
    from publishing_routes import _parse_publish_at, _sync_calendar_entries  # noqa: PLC0415

    publish_at = _parse_publish_at(req.publish_at)

    item_query = sb.table("content_items").select("id").eq("brief_id", req.brief_id)
    if req.content_item_id:
        item_query = item_query.eq("id", req.content_item_id)
    item_res = item_query.order("created_at", desc=True).limit(1).execute()
    if not item_res.data:
        raise HTTPException(404, f"No content items found for brief: {req.brief_id}")
    content_item_id = item_res.data[0]["id"]

    brief_res = (
        sb.table("content_briefs")
        .select("id,concept_id,format,created_by,content_angle,hook")
        .eq("id", req.brief_id)
        .single()
        .execute()
    )
    if not brief_res.data:
        raise HTTPException(404, f"Brief not found: {req.brief_id}")
    brief = brief_res.data
    concept_id: str = brief["concept_id"]

    job_id = _create_job(
        agent_type="scheduled_publish",
        concept_id=concept_id,
        payload={
            "content_item_id": content_item_id,
            "platforms": req.platforms,
            "platform_types": req.platform_types,
            "brief_id": req.brief_id,
            "publish_at": publish_at,
        },
    )

    # Store scheduled_at on the content item so the calendar can display it
    sb.table("content_items").update({
        "scheduled_at": publish_at,
        "updated_at": _now(),
    }).eq("id", content_item_id).execute()

    calendar_entries = _sync_calendar_entries(
        sb_client=sb,
        content_item_id=content_item_id,
        brief=brief,
        platforms=req.platforms,
        publish_at=publish_at,
    )

    # Email confirmation
    try:
        from notification_service import notify_post_scheduled  # noqa: PLC0415
        post_title = brief.get("content_angle") or brief.get("hook") or "Untitled"
        notify_post_scheduled(
            sb,
            title=post_title,
            platforms=req.platforms,
            publish_at=publish_at,
            platform_types=req.platform_types,
            brief_id=req.brief_id,
            content_item_id=content_item_id,
        )
    except Exception as _e:
        print(f"[agent_routes] schedule email failed: {_e}", flush=True)

    return {
        "ok": True,
        "job_id": job_id,
        "content_item_id": content_item_id,
        "publish_at": publish_at,
        "status": "scheduled",
        "calendar_entries": calendar_entries,
    }


@router.post("/master")
def trigger_master_agent(bg: BackgroundTasks):
    """
    Run the Master Agent cycle in the background:
      1. Recover stuck jobs (running >15 min)
      2. Scan all pipeline state across every concept
      3. Claude Sonnet analyses and produces prioritised actions
      4. Result stored in jobs table (agent_type='master')
    Returns a job_id immediately; poll /agents/master/status for the result.
    """
    import uuid as _uuid  # noqa: PLC0415

    job_id = str(_uuid.uuid4())

    def _run():
        try:
            from anthropic import Anthropic  # noqa: PLC0415
            from master_agent import run_master_agent  # noqa: PLC0415
            anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            run_master_agent(sb, anthropic)
        except Exception as exc:
            import traceback as _tb  # noqa: PLC0415
            print(f"[master_agent] background run failed: {exc}\n{_tb.format_exc()}", flush=True)

    bg.add_task(_run)
    return {"ok": True, "status": "queued", "message": "Master Agent cycle started in background"}


@router.get("/master/scheduler")
def get_scheduler_status():
    """Return current Master Agent scheduler state (interval, enabled, next/last run)."""
    from master_scheduler import get_state  # noqa: PLC0415
    return {"ok": True, "data": get_state()}


class SchedulerUpdate(BaseModel):
    interval_minutes: Optional[int] = None
    enabled: Optional[bool] = None


@router.post("/master/scheduler")
def update_scheduler(body: SchedulerUpdate):
    """Update the cron interval and/or enabled flag for the Master Agent scheduler."""
    from master_scheduler import set_interval, set_enabled, get_state  # noqa: PLC0415
    if body.interval_minutes is not None:
        set_interval(body.interval_minutes)
    if body.enabled is not None:
        set_enabled(body.enabled)
    return {"ok": True, "data": get_state()}


@router.get("/master/status")
def get_master_status():
    """
    Return the most recent Master Agent report.
    The report is stored in the jobs table with agent_type='master'.
    """
    res = (
        sb.table("jobs")
        .select("id,result,created_at,status")
        .eq("agent_type", "master")
        .eq("status", "succeeded")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return {"ok": False, "error": "No Master Agent report found — click 'Run Master Agent' to generate one"}
    row = res.data[0]
    return {
        "ok": True,
        "job_id": row["id"],
        "generated_at": row["created_at"],
        "report": row.get("result") or {},
    }


@router.get("/prompts")
def get_agent_prompts():
    """Return the live system prompts and models used by every agent."""
    from brand_pipeline import SYSTEM_PROMPT as research_prompt  # noqa: PLC0415
    from ideation_agent import SYSTEM_PROMPT as ideation_prompt, MODEL as ideation_model  # noqa: PLC0415
    from production_agent import SYSTEM_PROMPT as production_prompt, MODEL as production_model  # noqa: PLC0415
    from image_agent import PROMPT_SYSTEM as image_prompt, MODEL as image_model  # noqa: PLC0415
    from data_routes import DATA_AGENT_SYSTEM_PROMPT, DATA_AGENT_MODEL  # noqa: PLC0415
    from master_agent import MASTER_SYSTEM as master_prompt, MODEL as master_model  # noqa: PLC0415

    research_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    return {
        "agents": [
            {
                "id": "research",
                "name": "Research Agent",
                "model": research_model,
                "trigger": "Runs when a brand guidelines PDF is uploaded",
                "prompt": research_prompt,
            },
            {
                "id": "ideation",
                "name": "Ideation Agent",
                "model": ideation_model,
                "trigger": "Runs when \"Run Ideation\" is clicked on a brief card",
                "prompt": ideation_prompt,
            },
            {
                "id": "production",
                "name": "Production Copy Agent",
                "model": production_model,
                "trigger": "Runs when \"Write final copy\" is clicked on an approved concept",
                "prompt": production_prompt,
            },
            {
                "id": "data",
                "name": "Data Agent",
                "model": DATA_AGENT_MODEL,
                "trigger": "Runs on every message in the Analytics chat",
                "prompt": DATA_AGENT_SYSTEM_PROMPT,
            },
            {
                "id": "image",
                "name": "Image Prompt Agent",
                "model": image_model,
                "trigger": "Runs when \"Generate banner\" is clicked (AI image generation must be enabled)",
                "prompt": image_prompt,
            },
            {
                "id": "master",
                "name": "Master Agent",
                "model": master_model,
                "trigger": "Runs on demand via the Master Agent panel — scans all pipeline state and recovers stuck jobs",
                "prompt": master_prompt,
            },
        ]
    }


@router.get("/env-check")
def env_check():
    """Diagnostic: show which notification env vars are present in this process."""
    return {
        "RESEND_API_KEY":    "SET" if os.environ.get("RESEND_API_KEY") else "MISSING",
        "NOTIFY_FROM_EMAIL": os.environ.get("NOTIFY_FROM_EMAIL", "(default: noreply@artcaffemarket.co.ke)"),
        "NOTIFY_TO_EMAIL":   os.environ.get("NOTIFY_TO_EMAIL", "(default: pgitau@artcaffe.co.ke)"),
        "DASHBOARD_URL":     os.environ.get("DASHBOARD_URL", "(default)"),
    }


@router.post("/test-notification")
def test_notification():
    """Send a live test notification email and report the result."""
    import traceback  # noqa: PLC0415
    api_key = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("NOTIFY_FROM_EMAIL", "noreply@artcaffemarket.co.ke")
    to_email = os.environ.get("NOTIFY_TO_EMAIL", "pgitau@artcaffe.co.ke")
    if not api_key:
        return {"ok": False, "error": "RESEND_API_KEY not set", "from": from_addr}
    try:
        import resend  # noqa: PLC0415
        resend.api_key = api_key
        result = resend.Emails.send({
            "from": from_addr,
            "to": to_email,
            "subject": "Artcaffe AI — Live notification test",
            "html": "<p>Live test from FastAPI process.</p>",
        })
        return {"ok": True, "from": from_addr, "to": to_email, "result": str(result)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "trace": traceback.format_exc(), "from": from_addr}


@router.post("/analyze-asset")
def analyze_asset_endpoint(req: AnalyzeAssetRequest):
    """
    Run Claude Haiku vision analysis on an uploaded image asset.
    Updates the asset row with metadata and analysis_status='done'.
    Runs synchronously — typically completes in 5-15 seconds.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        metadata = _analyze_asset(sb=sb, anthropic=anthropic_client, asset_id=req.asset_id)
        return {"ok": True, "asset_id": req.asset_id, "metadata": metadata}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")


_NO_PROVIDER_PHRASES = ("not configured", "no image provider", "no source image", "ai generation is disabled")


@router.post("/generate-banner")
def generate_banner(req: BannerRequest):
    """
    Generate a marketing banner image for a content item.
    If no image API key is configured, falls back to Claude selecting
    the best existing asset from the library instead.
    Runs synchronously — may take 15-60 seconds for image generation.
    Returns the created/selected asset row with public_url for immediate display.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Multi-variant path: generate bar + split + solid from the same source image
    if req.generate_variants:
        try:
            assets = run_banner_variants(
                sb=sb,
                anthropic=anthropic_client,
                concept_id=req.concept_id,
                content_item_id=req.content_item_id,
                headline=req.headline,
                caption=req.caption,
                platform=req.platform,
                image_api_key=req.image_api_key,
                image_provider=req.image_provider,
                custom_system_prompt=req.custom_system_prompt,
            )
            if assets:
                return {"ok": True, "assets": assets, "asset": assets[0], "mode": "variants"}
            raise RuntimeError("No variants were generated")
        except RuntimeError as e:
            err_str = str(e)
            # Fall back to selecting the best existing asset when no provider/photo is available
            if any(phrase in err_str.lower() for phrase in _NO_PROVIDER_PHRASES):
                try:
                    asset = select_best_asset(
                        sb=sb,
                        anthropic=anthropic_client,
                        concept_id=req.concept_id,
                        content_item_id=req.content_item_id,
                        headline=req.headline,
                        caption=req.caption,
                        platform=req.platform,
                    )
                    asset = apply_overlay_to_asset(
                        sb=sb,
                        anthropic=anthropic_client,
                        asset=asset,
                        headline=req.headline,
                        concept_id=req.concept_id,
                        content_item_id=req.content_item_id,
                    )
                    return {"ok": True, "asset": asset, "assets": [asset], "mode": "selected"}
                except RuntimeError as fe:
                    raise HTTPException(status_code=400, detail=str(fe))
            raise HTTPException(status_code=400, detail=err_str)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Variant generation failed: {e}")

    # Try to generate a brand-new image first.
    try:
        asset = run_image_generation(
            sb=sb,
            anthropic=anthropic_client,
            concept_id=req.concept_id,
            content_item_id=req.content_item_id,
            headline=req.headline,
            caption=req.caption,
            platform=req.platform,
            image_api_key=req.image_api_key,
            image_provider=req.image_provider,
            custom_system_prompt=req.custom_system_prompt,
        )
        return {"ok": True, "asset": asset, "mode": "generated"}
    except RuntimeError as e:
        err_str = str(e)
        # No image provider / key configured → fall back to selecting an existing asset.
        if any(phrase in err_str.lower() for phrase in _NO_PROVIDER_PHRASES):
            try:
                asset = select_best_asset(
                    sb=sb,
                    anthropic=anthropic_client,
                    concept_id=req.concept_id,
                    content_item_id=req.content_item_id,
                    headline=req.headline,
                    caption=req.caption,
                    platform=req.platform,
                )
                asset = apply_overlay_to_asset(
                    sb=sb,
                    anthropic=anthropic_client,
                    asset=asset,
                    headline=req.headline,
                    concept_id=req.concept_id,
                    content_item_id=req.content_item_id,
                )
                return {"ok": True, "asset": asset, "mode": "selected"}
            except RuntimeError as fe:
                raise HTTPException(status_code=400, detail=str(fe))
        raise HTTPException(status_code=400, detail=err_str)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {e}")


@router.post("/generate-video")
def generate_video(req: VideoRequest):
    """
    Generate a short marketing video from an image using Runway Gen-4 Turbo.

    Flow:
      1. Resolve source image — uses req.image_url if provided, else auto-picks
         the most recent image asset attached to req.content_item_id.
      2. Claude Haiku writes a cinematic motion prompt.
      3. Runway image-to-video task is submitted + polled until complete.
      4. Video is downloaded, uploaded to Supabase Storage, and saved as an
         asset row (asset_type="video", generator="runway").
      5. Asset is attached to the content item's asset_ids array.

    Requires RUNWAYML_API_SECRET env var. Runs synchronously (~30-120s).
    Returns the saved asset row with public_url for immediate playback.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        asset = run_video_generation(
            sb=sb,
            anthropic=anthropic_client,
            concept_id=req.concept_id,
            content_item_id=req.content_item_id or None,
            headline=req.headline,
            caption=req.caption,
            platform=req.platform,
            image_url=req.image_url,
            runway_api_key=req.runway_api_key,
            model_override=req.runway_model,
        )
        return {"ok": True, "asset": asset, "mode": "video"}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video generation failed: {e}")


@router.post("/customize-headline")
def customize_headline(req: CustomizeHeadlineRequest):
    """
    Edit a content item's headline and re-bake it onto every attached image
    that has a clean pre-overlay source. Images without one (full-AI-rendered
    posters) are returned in skipped_assets — those need a full regenerate.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        result = customize_headline_text(
            sb=sb,
            anthropic=anthropic_client,
            content_item_id=req.content_item_id,
            headline=req.headline,
        )
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Customize headline failed: {e}")


@router.post("/customize-audio")
def customize_audio(req: CustomizeAudioRequest):
    """
    Mux a chosen audio asset onto the content item's attached video, replacing
    its existing audio track. Meta's API can't attach IG/FB's licensed music
    catalog directly, so this bakes the track into the video file before publish.
    """
    item_res = (
        sb.table("content_items")
        .select("id,asset_ids")
        .eq("id", req.content_item_id)
        .single()
        .execute()
    )
    if not item_res.data:
        raise HTTPException(status_code=404, detail=f"Content item not found: {req.content_item_id}")
    item = item_res.data
    asset_ids = item.get("asset_ids") or []

    video_res = (
        sb.table("assets")
        .select("id,public_url,platform")
        .in_("id", asset_ids)
        .eq("asset_type", "video")
        .limit(1)
        .execute()
    )
    if not video_res.data:
        raise HTTPException(status_code=400, detail="No video asset attached to this content item")
    video_asset = video_res.data[0]

    audio_res = (
        sb.table("assets")
        .select("id,public_url")
        .eq("id", req.audio_asset_id)
        .eq("asset_type", "audio")
        .single()
        .execute()
    )
    if not audio_res.data or not audio_res.data.get("public_url"):
        raise HTTPException(status_code=404, detail=f"Audio asset not found: {req.audio_asset_id}")
    audio_asset = audio_res.data

    try:
        import httpx as _httpx  # noqa: PLC0415
        video_bytes = _httpx.get(video_asset["public_url"], timeout=60.0).content
        audio_bytes = _httpx.get(audio_asset["public_url"], timeout=60.0).content

        muxed_bytes = mux_audio_into_video(video_bytes, audio_bytes)

        # Overwrite the video's existing storage object in place — same
        # bucket/path means its public_url never changes.
        bucket, storage_path = _bucket_and_path_from_url(video_asset["public_url"])
        _upload_in_place(sb, bucket, storage_path, muxed_bytes, "video/mp4")

        new_metadata = {"audio_asset_id": req.audio_asset_id}
        sb.table("assets").update({
            "metadata": new_metadata,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", video_asset["id"]).execute()

        return {"ok": True, "asset": {**video_asset, "metadata": new_metadata}}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio mux failed: {e}")


@router.post("/customize-asset-layout")
def customize_asset_layout_endpoint(req: CustomizeAssetLayoutRequest):
    """
    Re-composite a single image asset's headline/body text at explicit
    positions from the Assets page's drag-to-position layout editor.
    """
    layout = {
        "headline_text": req.headline.text,
        "headline_x_pct": req.headline.x_pct,
        "headline_y_pct": req.headline.y_pct,
        "headline_size_pct": req.headline.size_pct,
        "headline_color": req.headline.color,
        "headline_align": req.headline.align,
        "body_text": req.body.text,
        "body_x_pct": req.body.x_pct,
        "body_y_pct": req.body.y_pct,
        "body_size_pct": req.body.size_pct,
        "body_color": req.body.color,
        "body_align": req.body.align,
        "scrim_position": req.scrim_position,
        "scrim_height_pct": req.scrim_height_pct,
        "scrim_opacity": req.scrim_opacity,
    }
    try:
        return customize_asset_layout(sb=sb, asset_id=req.asset_id, layout=layout)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Layout customization failed: {e}")


@router.post("/standardize-product-image")
def standardize_product_image_endpoint(req: StandardizeProductRequest):
    """
    Reformat an image asset into a standard 1000x1000 product shot centered
    on a pure white background. mode="openai" runs it through OpenAI first
    to actually remove/replace the background; mode="simple" just resizes
    and pads (works well when the photo is already on a plain background).
    """
    try:
        return standardize_product_image(
            sb=sb,
            asset_id=req.asset_id,
            mode=req.mode,
            openai_api_key=req.openai_api_key,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Product standardization failed: {e}")

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
from image_agent import run_image_generation, run_banner_variants, select_best_asset, apply_overlay_to_asset
from image_analysis_agent import analyze_asset as _analyze_asset
from video_agent import run_video_generation

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


class PublishNowRequest(BaseModel):
    brief_id: str
    platforms: list[str]  # e.g. ["instagram", "facebook", "linkedin", "google_ads"]


class SchedulePublishRequest(BaseModel):
    brief_id: str
    platforms: list[str]
    publish_at: str  # ISO 8601 UTC timestamp, e.g. "2026-06-10T09:00:00Z"


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
            "publish_at": req.publish_at,
        },
    )

    # Store scheduled_at on the content item so the calendar can display it
    sb.table("content_items").update({
        "scheduled_at": req.publish_at,
        "updated_at": _now(),
    }).eq("id", content_item_id).execute()

    return {
        "ok": True,
        "job_id": job_id,
        "content_item_id": content_item_id,
        "publish_at": req.publish_at,
        "status": "scheduled",
    }


@router.get("/prompts")
def get_agent_prompts():
    """Return the live system prompts and models used by every agent."""
    from brand_pipeline import SYSTEM_PROMPT as research_prompt  # noqa: PLC0415
    from ideation_agent import SYSTEM_PROMPT as ideation_prompt, MODEL as ideation_model  # noqa: PLC0415
    from production_agent import SYSTEM_PROMPT as production_prompt, MODEL as production_model  # noqa: PLC0415
    from image_agent import PROMPT_SYSTEM as image_prompt, MODEL as image_model  # noqa: PLC0415
    from data_routes import DATA_AGENT_SYSTEM_PROMPT, DATA_AGENT_MODEL  # noqa: PLC0415

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


_NO_PROVIDER_PHRASES = ("not configured", "no image provider")


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
            )
            if not assets:
                raise RuntimeError("No variants were generated")
            return {"ok": True, "assets": assets, "asset": assets[0], "mode": "variants"}
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
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

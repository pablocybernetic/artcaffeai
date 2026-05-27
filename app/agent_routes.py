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
from image_agent import run_image_generation

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
def update_item_status(item_id: str, body: ItemStatusUpdate):
    """
    Update the status of a content item.

    Allowed statuses: approved | rejected | draft | pending_review
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

    return {"ok": True, "updated": res.data[0]}


@router.post("/generate-banner")
def generate_banner(req: BannerRequest):
    """
    Generate a marketing banner image for a content item.
    Runs synchronously — may take 15-60 seconds depending on provider.
    Returns the created asset row with public_url for immediate display.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        asset = run_image_generation(
            sb=sb,
            anthropic=anthropic_client,
            concept_id=req.concept_id,
            content_item_id=req.content_item_id,
            headline=req.headline,
            caption=req.caption,
            platform=req.platform,
        )
        return {"ok": True, "asset": asset}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation failed: {e}")

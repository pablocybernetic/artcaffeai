"""
budget_routes.py
----------------
FastAPI router for the Budget Agent.

Prefix: /agents/budget
Auth:   X-Api-Key header

Endpoints:
  POST /agents/budget/analyze                    — run analysis for one or all concepts
  GET  /agents/budget/recommendations            — latest recommendation per concept
  GET  /agents/budget/recommendations/{concept_id} — latest recommendation for one concept
  GET  /agents/budget/alerts                     — unresolved alerts across all concepts
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

SUPABASE_URL             = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY                  = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(
    prefix="/agents/budget",
    dependencies=[Depends(require_api_key)],
)


class AnalyzeRequest(BaseModel):
    concept_id: Optional[str] = None  # omit to analyze all concepts


@router.post("/analyze")
def analyze_budget(req: AnalyzeRequest):
    """
    Run budget pacing analysis for one or all concepts.
    Uses Claude Haiku to generate reallocation recommendations.
    Writes alerts to budget_alerts and saves results to budget_recommendations.
    Typically completes in 5–15 seconds.
    """
    from anthropic import Anthropic  # noqa: PLC0415
    from budget_agent import run_budget_analysis  # noqa: PLC0415

    anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        result = run_budget_analysis(
            sb=sb,
            anthropic=anthropic,
            concept_id=req.concept_id or None,
        )
        return {"ok": True, **result}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Budget analysis failed: {e}")


@router.get("/recommendations")
def list_recommendations():
    """Return the most recent recommendation for each concept (up to 10 total)."""
    try:
        res = (
            sb.table("budget_recommendations")
            .select("*")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        # Deduplicate: keep only the latest row per concept
        seen: set[str] = set()
        latest: list[dict] = []
        for row in (res.data or []):
            cid = row.get("concept_id")
            if cid and cid not in seen:
                seen.add(cid)
                latest.append(row)
        return {"ok": True, "recommendations": latest}
    except Exception as e:
        return {"ok": False, "recommendations": [], "error": str(e)}


@router.get("/recommendations/{concept_id}")
def get_recommendation(concept_id: str):
    """Return the latest recommendation for a specific concept."""
    try:
        res = (
            sb.table("budget_recommendations")
            .select("*")
            .eq("concept_id", concept_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return {"ok": True, "recommendation": res.data[0] if res.data else None}
    except Exception as e:
        return {"ok": False, "recommendation": None, "error": str(e)}


@router.get("/alerts")
def list_alerts():
    """Return all unresolved budget alerts, most recent first."""
    try:
        res = (
            sb.table("budget_alerts")
            .select("*, budget_allocations(concept_id, platform, allocated_usd, spent_usd)")
            .eq("is_resolved", False)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return {"ok": True, "alerts": res.data or []}
    except Exception as e:
        return {"ok": False, "alerts": [], "error": str(e)}

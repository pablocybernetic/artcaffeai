"""
budget_routes.py
----------------
FastAPI router for the Budget Agent.

Prefix: /agents/budget
Auth:   X-Api-Key header

Endpoints:
  POST  /agents/budget/analyze                      — run analysis for one or all concepts
  GET   /agents/budget/recommendations              — latest recommendation per concept
  GET   /agents/budget/recommendations/{concept_id} — latest recommendation for one concept
  GET   /agents/budget/alerts                       — unresolved alerts across all concepts
  PATCH /agents/budget/alerts/{alert_id}/resolve    — mark an alert resolved
  GET   /agents/budget/allocations                  — list all allocations with concept names
  POST  /agents/budget/allocations                  — create a new allocation
  PATCH /agents/budget/allocations/{id}             — update allocated_usd / spent_usd / period
  DELETE /agents/budget/allocations/{id}            — delete an allocation
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

SUPABASE_URL              = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY                   = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(
    prefix="/agents/budget",
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    concept_id: Optional[str] = None


class AllocationCreate(BaseModel):
    concept_id: str
    platform: str
    period_start: str   # "YYYY-MM-DD"
    period_end: str     # "YYYY-MM-DD"
    allocated_usd: float
    spent_usd: float = 0.0


class AllocationUpdate(BaseModel):
    allocated_usd: Optional[float] = None
    spent_usd: Optional[float] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
@router.post("/analyze")
def analyze_budget(req: AnalyzeRequest):
    """
    Run budget pacing analysis for one or all concepts.
    Syncs spent_usd from BigQuery first, then uses Claude Haiku to generate
    reallocation recommendations. Writes alerts and saves results to DB.
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


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------
@router.get("/recommendations")
def list_recommendations():
    """Return the most recent recommendation for each concept."""
    try:
        res = (
            sb.table("budget_recommendations")
            .select("*")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
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


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
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


@router.patch("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: str):
    """Mark a single alert as resolved."""
    res = (
        sb.table("budget_alerts")
        .update({"is_resolved": True})
        .eq("id", alert_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True, "id": alert_id}


# ---------------------------------------------------------------------------
# Allocation CRUD
# ---------------------------------------------------------------------------
@router.get("/allocations")
def list_allocations():
    """
    Return all budget allocations with concept name resolved.
    Sorted by period_start desc, then concept.
    """
    try:
        alloc_res = (
            sb.table("budget_allocations")
            .select("*, concepts(id,name,key)")
            .order("period_start", desc=True)
            .execute()
        )
        rows = []
        for a in (alloc_res.data or []):
            concept = a.pop("concepts", None) or {}
            rows.append({
                **a,
                "concept_name": concept.get("name", ""),
                "concept_key":  concept.get("key", ""),
            })
        return {"ok": True, "allocations": rows}
    except Exception as e:
        return {"ok": False, "allocations": [], "error": str(e)}


@router.post("/allocations")
def create_allocation(req: AllocationCreate):
    """Create a new budget allocation row."""
    row = {
        "id":            str(uuid.uuid4()),
        "concept_id":    req.concept_id,
        "platform":      req.platform,
        "period_start":  req.period_start,
        "period_end":    req.period_end,
        "allocated_usd": req.allocated_usd,
        "spent_usd":     req.spent_usd,
        "created_at":    _now(),
        "updated_at":    _now(),
    }
    try:
        res = sb.table("budget_allocations").insert(row).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="Insert returned no data")
        return {"ok": True, "allocation": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/allocations/{allocation_id}")
def update_allocation(allocation_id: str, req: AllocationUpdate):
    """Update allocated_usd, spent_usd, and/or period dates on an allocation."""
    updates: dict = {"updated_at": _now()}
    if req.allocated_usd is not None:
        updates["allocated_usd"] = req.allocated_usd
    if req.spent_usd is not None:
        updates["spent_usd"] = req.spent_usd
    if req.period_start is not None:
        updates["period_start"] = req.period_start
    if req.period_end is not None:
        updates["period_end"] = req.period_end

    if len(updates) == 1:  # only updated_at
        raise HTTPException(status_code=400, detail="No fields to update")

    res = (
        sb.table("budget_allocations")
        .update(updates)
        .eq("id", allocation_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Allocation not found")
    return {"ok": True, "allocation": res.data[0]}


@router.delete("/allocations/{allocation_id}")
def delete_allocation(allocation_id: str):
    """Delete a budget allocation and its associated alerts."""
    # Resolve alerts first (FK constraint)
    try:
        sb.table("budget_alerts").update({"is_resolved": True}).eq(
            "allocation_id", allocation_id
        ).execute()
    except Exception:
        pass

    res = (
        sb.table("budget_allocations")
        .delete()
        .eq("id", allocation_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Allocation not found")
    return {"ok": True, "deleted": allocation_id}

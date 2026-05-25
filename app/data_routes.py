"""
Data Agent routes for Artcaffe FastAPI service.

Drop this file into /opt/artcaffe/app/ next to app.py, then in app.py add:

    from data_routes import router as data_router
    app.include_router(data_router)

Endpoints:
    POST /data/ga4/snapshot   -> pull last 7d GA4 metrics, upsert into
                                 public.platform_data_snapshots
    POST /data/chat           -> answer questions about a concept's snapshots
                                 using Claude Haiku 4.5

Env vars required:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (already set)
    FASTAPI_API_KEY                            (already set)
    ANTHROPIC_API_KEY                          (new)
    GA4_SERVICE_ACCOUNT_JSON                   (new — full JSON string)
    GA4_PROPERTY_IDS                           (new — JSON: {"gastro_bar": "123456789", ...})
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Shared config (mirrors app.py)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GA4_PROPERTY_IDS = json.loads(os.environ.get("GA4_PROPERTY_IDS", "{}"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/data", dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SnapshotRequest(BaseModel):
    concept_id: Optional[str] = None
    platform: Optional[str] = None  # defaults to "ga4"


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    concept_id: Optional[str] = None
    messages: list[ChatMessage]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _concept_key(concept_id: str) -> Optional[str]:
    row = (
        sb.table("concepts")
        .select("key")
        .eq("id", concept_id)
        .maybe_single()
        .execute()
    )
    return (row.data or {}).get("key") if row and row.data else None


def _ga4_property_for(concept_id: Optional[str]) -> Optional[str]:
    if not concept_id:
        return GA4_PROPERTY_IDS.get("default")
    key = _concept_key(concept_id)
    return GA4_PROPERTY_IDS.get(key or "", GA4_PROPERTY_IDS.get("default"))


def _pull_ga4_last_7d(property_id: str) -> dict[str, Any]:
    """Pull last 7d core metrics from GA4 Data API."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )
    from google.oauth2 import service_account

    creds_json = os.environ["GA4_SERVICE_ACCOUNT_JSON"]
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    client = BetaAnalyticsDataClient(credentials=creds)

    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        dimensions=[Dimension(name="date"), Dimension(name="sessionDefaultChannelGroup")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration"),
        ],
    )
    resp = client.run_report(req)

    rows = []
    totals = {"sessions": 0, "activeUsers": 0, "screenPageViews": 0}
    for r in resp.rows:
        d = {h.name: v.value for h, v in zip(resp.dimension_headers, r.dimension_values)}
        m = {h.name: float(v.value or 0) for h, v in zip(resp.metric_headers, r.metric_values)}
        rows.append({**d, **m})
        totals["sessions"] += int(m.get("sessions", 0))
        totals["activeUsers"] += int(m.get("activeUsers", 0))
        totals["screenPageViews"] += int(m.get("screenPageViews", 0))

    return {
        "property_id": property_id,
        "range": {"start": "7daysAgo", "end": "yesterday"},
        "totals": totals,
        "rows": rows[:200],  # cap payload
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/ga4/snapshot")
def ga4_snapshot(req: SnapshotRequest):
    if not req.concept_id:
        # snapshot all concepts that have a mapped property
        concepts = sb.table("concepts").select("id,key").execute().data or []
        out = []
        for c in concepts:
            pid = GA4_PROPERTY_IDS.get(c["key"])
            if not pid:
                continue
            try:
                out.append(_snapshot_one(c["id"], pid))
            except Exception as e:  # noqa: BLE001
                out.append({"concept_id": c["id"], "error": str(e)})
        return {"ok": True, "snapshots": out}

    pid = _ga4_property_for(req.concept_id)
    if not pid:
        raise HTTPException(404, "No GA4 property mapped for this concept")
    return {"ok": True, "snapshot": _snapshot_one(req.concept_id, pid)}


def _snapshot_one(concept_id: str, property_id: str) -> dict[str, Any]:
    summary = _pull_ga4_last_7d(property_id)
    today = date.today().isoformat()
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "ga4",
            "snapshot_date": today,
            "summary_json": summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()
    return {"concept_id": concept_id, "snapshot_date": today, "totals": summary["totals"]}


@router.post("/chat")
def chat(req: ChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    # Load latest snapshots for this concept (last 14 days, all platforms)
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    q = (
        sb.table("platform_data_snapshots")
        .select("platform,snapshot_date,summary_json")
        .gte("snapshot_date", cutoff)
        .order("snapshot_date", desc=True)
        .limit(30)
    )
    if req.concept_id:
        q = q.eq("concept_id", req.concept_id)
    snapshots = q.execute().data or []

    context_blob = json.dumps(snapshots, default=str)[:50_000]
    system = (
        "You are the Artcaffe Data Agent. Answer questions about the user's "
        "platform performance using ONLY the snapshot JSON provided. If the "
        "data doesn't contain the answer, say so plainly. Be concise. When "
        "useful, suggest a chart as a JSON object on its own line prefixed "
        "with `CHART:` containing {type, title, data}."
        f"\n\nSNAPSHOTS:\n{context_blob}"
    )

    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=system,
        messages=[{"role": m.role, "content": m.content} for m in req.messages],
    )

    text = "".join(getattr(b, "text", "") for b in resp.content)
    chart = None
    if "CHART:" in text:
        head, _, tail = text.partition("CHART:")
        try:
            chart = json.loads(tail.strip().splitlines()[0])
            text = head.strip()
        except Exception:  # noqa: BLE001
            pass

    return {"answer": text, "chart": chart}

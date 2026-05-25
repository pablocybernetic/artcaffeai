"""
FastAPI service for Artcaffe brand pipeline.

Aligned to the Lovable frontend's `jobs` table schema:
  - columns: id, concept_id, agent_type, status, input_payload,
             result, error_message, created_at, updated_at,
             started_at, finished_at
  - frontend filters jobs by agent_type = 'research'

Endpoints:
  POST /brand-guidelines/process   (called by frontend serverFn)
  POST /jobs                       (legacy/test: create + run)
  GET  /jobs/{job_id}              (status poll)
  GET  /healthz
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

from job_runner import run_job

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")  # shared with Lovable serverFn

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = FastAPI(title="Artcaffe Brand API", version="2.0")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return  # auth disabled
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ProcessRequest(BaseModel):
    """Body posted by the Lovable serverFn notifyBrandGuidelines."""
    task: str = "extract_brand_context"
    concept_id: str
    file_path: str
    file_name: str
    mime_type: str
    job_id: Optional[str] = None  # frontend pre-created the row


class CreateJobRequest(BaseModel):
    concept_id: str
    file_path: str
    file_name: str
    mime_type: str = "application/pdf"


class JobOut(BaseModel):
    id: str
    concept_id: str
    agent_type: str
    status: str
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_job(job_id: str) -> dict:
    res = (
        sb.table("jobs")
        .select("id,concept_id,agent_type,status,result,error_message,"
                "created_at,started_at,finished_at")
        .eq("id", job_id)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Job not found")
    return res.data


def _insert_job(req: CreateJobRequest) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "concept_id": req.concept_id,
        "agent_type": "research",
        "status": "pending",
        "input_payload": {
            "task": "extract_brand_context",
            "file_path": req.file_path,
            "file_name": req.file_name,
            "mime_type": req.mime_type,
        },
        "created_at": _now(),
    }
    sb.table("jobs").insert(row).execute()
    return _fetch_job(row["id"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": _now()}


@app.post("/brand-guidelines/process", dependencies=[Depends(require_api_key)])
def brand_guidelines_process(req: ProcessRequest, bg: BackgroundTasks):
    """
    Called by the Lovable frontend after it uploads to Storage and inserts
    a `jobs` row. We process the existing row in-place (no duplicate insert).
    If job_id is missing (manual curl), we create a new row.
    """
    if req.job_id:
        job = _fetch_job(req.job_id)
    else:
        job = _insert_job(CreateJobRequest(
            concept_id=req.concept_id,
            file_path=req.file_path,
            file_name=req.file_name,
            mime_type=req.mime_type,
        ))

    bg.add_task(run_job, job["id"])
    return {"ok": True, "job_id": job["id"], "status": "queued"}


@app.post("/jobs", response_model=JobOut, dependencies=[Depends(require_api_key)])
def create_job(req: CreateJobRequest, bg: BackgroundTasks):
    """Legacy/test endpoint: create a job row and process it inline."""
    job = _insert_job(req)
    bg.add_task(run_job, job["id"])
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    return _fetch_job(job_id)

"""
app.py
------
FastAPI application entry point for the ArtCaffe Brand Pipeline API.

Endpoints:
  POST /jobs          — enqueue a brand-guidelines processing job
  GET  /jobs/{job_id} — check job status
  GET  /health        — liveness probe

The API validates the request, inserts a row into `jobs`, then immediately
processes it in a BackgroundTask so the caller gets a job_id right away.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from anthropic import Anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client

from job_runner import run_job

# ---------- env / singletons ----------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BRAND_BUCKET = os.environ.get("BRAND_BUCKET", "brand-guidelines")
BRAND_MODEL = os.environ.get("BRAND_MODEL", "claude-sonnet-4-20250514")

_sb: Optional[Client] = None
_anthropic: Optional[Anthropic] = None


def get_sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _sb


def get_anthropic() -> Anthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic


# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up connections on startup
    get_sb()
    get_anthropic()
    yield


# ---------- app ----------
app = FastAPI(
    title="ArtCaffe Brand Pipeline API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- schemas ----------
class CreateJobRequest(BaseModel):
    concept_id: str = Field(..., description="UUID of the concept")
    file_path: str = Field(..., description="Storage path within the brand-guidelines bucket")
    file_name: str = Field(default="", description="Original filename (used for extension detection)")
    mime_type: str = Field(default="", description="MIME type of the uploaded file")


class JobResponse(BaseModel):
    job_id: str
    status: str
    task: str
    concept_id: Optional[str]
    result: Optional[dict[str, Any]]
    error: Optional[str]
    queued_at: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


# ---------- routes ----------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobResponse)
def create_job(req: CreateJobRequest, background_tasks: BackgroundTasks):
    sb = get_sb()
    job_id = str(uuid.uuid4())

    # Insert the job row
    res = sb.table("jobs").insert({
        "id": job_id,
        "task": "brand_guidelines.process",
        "status": "pending",
        "concept_id": req.concept_id,
        "payload": {
            "file_path": req.file_path,
            "file_name": req.file_name,
            "mime_type": req.mime_type,
        },
    }).execute()

    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create job")

    job = res.data[0]

    # Process in the background immediately
    background_tasks.add_task(
        run_job,
        sb,
        get_anthropic(),
        bucket=BRAND_BUCKET,
        model=BRAND_MODEL,
        job_id=job_id,
    )

    return JobResponse(
        job_id=job["id"],
        status=job["status"],
        task=job["task"],
        concept_id=job.get("concept_id"),
        result=job.get("result"),
        error=job.get("error"),
        queued_at=job.get("queued_at") or job.get("created_at"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str):
    sb = get_sb()
    res = sb.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = res.data[0]
    return JobResponse(
        job_id=job["id"],
        status=job["status"],
        task=job["task"],
        concept_id=job.get("concept_id"),
        result=job.get("result"),
        error=job.get("error"),
        queued_at=job.get("queued_at") or job.get("created_at"),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )

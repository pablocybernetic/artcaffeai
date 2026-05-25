"""
job_runner.py
-------------
Bridges the `jobs` table to `brand_pipeline.run_pipeline`.

Two ways to use it:

  A) Inline (called from FastAPI BackgroundTasks in app.py):
        run_job(sb, anthropic, bucket, model, job_id=...)

  B) Standalone poller (run as `python job_runner.py` on the VM via
     a second systemd unit). Polls `jobs` for pending rows and processes
     them one at a time. Useful if you ever want to decouple the API
     from the worker.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic
from supabase import Client, create_client

from brand_pipeline import PipelineInput, run_pipeline

POLL_INTERVAL_SECONDS = 5
SUPPORTED_TASKS = {"brand_guidelines.process"}


# ---------- single-job entry point ----------
def run_job(
    sb: Client,
    anthropic: Anthropic,
    *,
    bucket: str,
    model: str,
    job_id: str,
) -> None:
    """Process one job by id. Updates the row's status/result/error."""
    job = _fetch_job(sb, job_id)
    if job is None:
        return

    _mark_running(sb, job_id)

    try:
        if job["task"] not in SUPPORTED_TASKS:
            raise ValueError(f"Unsupported task: {job['task']}")

        payload: dict[str, Any] = job.get("payload") or {}
        inp = PipelineInput(
            concept_id=job["concept_id"] or payload["concept_id"],
            file_path=payload["file_path"],
            file_name=payload.get("file_name", ""),
            mime_type=payload.get("mime_type", ""),
        )
        result = run_pipeline(
            sb=sb, anthropic=anthropic, bucket=bucket, model=model, inp=inp
        )
        _mark_succeeded(
            sb,
            job_id,
            {
                "brand_context_id": result.brand_context_id,
                "version": result.version,
                "chars_extracted": result.chars_extracted,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _mark_failed(sb, job_id, f"{exc}\n{traceback.format_exc()}")


# ---------- standalone poller ----------
def main() -> None:
    sb = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )
    anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    bucket = os.environ.get("BRAND_BUCKET", "brand-guidelines")
    model = os.environ.get("BRAND_MODEL", "claude-sonnet-4-20250514")

    print("[job_runner] polling jobs table...")
    while True:
        job = _claim_next_pending(sb)
        if job is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        print(f"[job_runner] processing {job['id']} ({job['task']})")
        run_job(sb, anthropic, bucket=bucket, model=model, job_id=job["id"])


# ---------- db helpers ----------
def _fetch_job(sb: Client, job_id: str) -> dict[str, Any] | None:
    res = sb.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    return res.data[0] if res.data else None


def _claim_next_pending(sb: Client) -> dict[str, Any] | None:
    res = (
        sb.table("jobs")
        .select("*")
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    job = res.data[0]
    upd = (
        sb.table("jobs")
        .update({
            "status": "running",
            "started_at": _now(),
            "attempts": (job.get("attempts") or 0) + 1,
        })
        .eq("id", job["id"])
        .eq("status", "pending")
        .execute()
    )
    return job if upd.data else None


def _mark_running(sb: Client, job_id: str) -> None:
    sb.table("jobs").update({"status": "running", "started_at": _now()}).eq(
        "id", job_id
    ).execute()


def _mark_succeeded(sb: Client, job_id: str, result: dict[str, Any]) -> None:
    sb.table("jobs").update(
        {"status": "succeeded", "finished_at": _now(), "result": result, "error": None}
    ).eq("id", job_id).execute()


def _mark_failed(sb: Client, job_id: str, error: str) -> None:
    sb.table("jobs").update(
        {"status": "failed", "finished_at": _now(), "error": error[:8000]}
    ).eq("id", job_id).execute()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()

"""
Job runner — bridges the `jobs` table to brand_pipeline.run_pipeline.

Aligned schema (matches Lovable frontend):
  jobs(id, concept_id, agent_type, status, input_payload, result,
       error_message, created_at, updated_at, started_at, finished_at)

Statuses:  pending -> running -> succeeded | failed

Usage:
  - Inline (from FastAPI BackgroundTasks):  run_job(job_id)
  - Standalone poller (systemd worker):     python job_runner.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic
from supabase import Client, create_client

from brand_pipeline import run_pipeline, PipelineInput

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BRAND_BUCKET = os.environ.get("BRAND_BUCKET", "brand-guidelines")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
POLL_INTERVAL_SECONDS = float(os.environ.get("WORKER_POLL_INTERVAL", "5"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_running(job_id: str) -> None:
    sb.table("jobs").update({
        "status": "running",
        "started_at": _now(),
        "updated_at": _now(),
        "error_message": None,
    }).eq("id", job_id).execute()


def _mark_succeeded(job_id: str, result: dict) -> None:
    sb.table("jobs").update({
        "status": "succeeded",
        "result": result,
        "finished_at": _now(),
        "updated_at": _now(),
        "error_message": None,
    }).eq("id", job_id).execute()


def _mark_failed(job_id: str, err: str) -> None:
    sb.table("jobs").update({
        "status": "failed",
        "error_message": err[:4000],
        "finished_at": _now(),
        "updated_at": _now(),
    }).eq("id", job_id).execute()


def _fetch(job_id: str) -> Optional[dict]:
    res = (
        sb.table("jobs")
        .select("id,concept_id,agent_type,status,input_payload")
        .eq("id", job_id)
        .single()
        .execute()
    )
    return res.data


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def run_job(job_id: str) -> dict:
    """Process a single job by id. Safe to call from FastAPI BackgroundTasks."""
    job = _fetch(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}

    if job["status"] not in ("pending", "running"):
        return {"ok": True, "skipped": True, "status": job["status"]}

    if job.get("agent_type") != "research":
        return {"ok": True, "skipped": True, "reason": "not_research"}

    payload = job.get("input_payload") or {}
    concept_id = job["concept_id"]

    try:
        _mark_running(job_id)

        result = run_pipeline(
            sb=sb,
            anthropic=anthropic_client,
            bucket=BRAND_BUCKET,
            model=ANTHROPIC_MODEL,
            inp=PipelineInput(
                concept_id=concept_id,
                file_path=payload["file_path"],
                file_name=payload.get("file_name", "guidelines.pdf"),
                mime_type=payload.get("mime_type", "application/pdf"),
            ),
        )

        result_dict = {
            "brand_context_id": result.brand_context_id,
            "version": result.version,
            "chars_extracted": result.chars_extracted,
        }
        _mark_succeeded(job_id, result_dict)
        return {"ok": True, "job_id": job_id, "result": result_dict}
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
        try:
            _mark_failed(job_id, err)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "job_id": job_id, "error": err}


# ---------------------------------------------------------------------------
# Standalone poller (systemd worker mode)
# ---------------------------------------------------------------------------
def _claim_next_pending() -> Optional[dict]:
    res = (
        sb.table("jobs")
        .select("id")
        .eq("status", "pending")
        .eq("agent_type", "research")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def main() -> None:
    print(f"[job_runner] poller started (interval={POLL_INTERVAL_SECONDS}s)",
          flush=True)
    while True:
        try:
            job = _claim_next_pending()
            if job:
                print(f"[job_runner] picking up {job['id']}", flush=True)
                run_job(job["id"])
            else:
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("[job_runner] stopping", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[job_runner] poll error: {e}", file=sys.stderr, flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # one-shot: `python job_runner.py <job_id>`
        print(run_job(sys.argv[1]))
    else:
        main()

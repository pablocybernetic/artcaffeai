"""
publish_scheduler.py
-----------------------
In-process asyncio cron that fires `scheduled_publish` jobs at their
publish_at time. Runs inside every uvicorn worker, alongside the other
schedulers (locations_scheduler, meta_sync_scheduler, etc.) so scheduled
posting no longer depends on the separate `artcaffe-worker.service`
standalone poller in job_runner.py's main() -- that unit is easy to
forget to enable (its own comment claims it's unneeded when
BackgroundTasks are used, which is misleading for this one job type),
and was in fact left disabled: 3 posts scheduled for 2026-07-06/07 sat
"pending" for two months and never published. This scheduler is the
fix, and runs unconditionally -- no separate service to remember.

job_runner.py's run_job() is reused as-is for actual execution; this
module only owns (a) safely claiming due jobs across the 2 uvicorn
workers (API_WORKERS=2) via a conditional status='pending'->'running'
update, and (b) refusing to auto-fire a job whose publish window was
missed by more than STALE_AFTER_SECONDS -- content scheduled days/weeks
ago may reference stale promotions/pricing/context, so it's marked
failed with a clear reason instead of silently going out, and someone
can manually re-publish it from the UI if it's still relevant.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

_TICK_SECONDS = 30  # how often to check for due scheduled posts
_STALE_AFTER_SECONDS = 24 * 3600  # a missed publish window this old needs a human, not an auto-fire

_task: Optional[asyncio.Task] = None
_sb: Optional[Client] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _overdue_seconds(publish_at: Optional[str]) -> float:
    """Seconds past the publish window, or 0 if not yet due / unparseable
    (treated as due-now, matching job_runner._is_publish_due's behavior)."""
    if not publish_at:
        return 0.0
    dt = _parse_iso(publish_at)
    if dt is None:
        return 0.0
    return (_now() - dt).total_seconds()


def _claim(job_id: str) -> bool:
    """Atomically flips pending -> running, only if still pending — guards
    against both uvicorn workers grabbing the same due job on one tick."""
    res = (
        _sb.table("jobs")
        .update({"status": "running", "started_at": _now().isoformat(), "updated_at": _now().isoformat()})
        .eq("id", job_id)
        .eq("status", "pending")
        .execute()
    )
    return bool(res.data)


def _mark_stale_failed(job_id: str, overdue_seconds: float) -> bool:
    """Same atomic claim, but immediately fails the job instead of running
    it — for a publish window missed by more than _STALE_AFTER_SECONDS."""
    days = overdue_seconds / 86400
    res = (
        _sb.table("jobs")
        .update({
            "status": "failed",
            "error_message": (
                f"Missed its publish window by {days:.1f} day(s) — "
                "auto-publish skipped to avoid posting stale content. "
                "Re-publish manually from the content item if it's still relevant."
            ),
            "finished_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
        })
        .eq("id", job_id)
        .eq("status", "pending")
        .execute()
    )
    return bool(res.data)


def _due_scheduled_publish_jobs(sb: Client) -> list[dict]:
    res = (
        sb.table("jobs")
        .select("id,input_payload")
        .eq("status", "pending")
        .eq("agent_type", "scheduled_publish")
        .order("created_at", desc=False)
        .limit(20)
        .execute()
    )
    due = []
    for row in res.data or []:
        publish_at = (row.get("input_payload") or {}).get("publish_at")
        overdue = _overdue_seconds(publish_at)
        if overdue >= 0:
            due.append({"id": row["id"], "overdue_seconds": overdue})
    return due


async def _scheduler_loop() -> None:
    print(f"[publish_scheduler] loop started — tick={_TICK_SECONDS}s stale_after={_STALE_AFTER_SECONDS}s", flush=True)
    while True:
        await asyncio.sleep(_TICK_SECONDS)
        loop = asyncio.get_event_loop()
        try:
            due_jobs = await loop.run_in_executor(None, _due_scheduled_publish_jobs, _sb)
        except Exception as exc:  # noqa: BLE001
            print(f"[publish_scheduler] poll error: {exc}", flush=True)
            continue

        for job in due_jobs:
            job_id = job["id"]
            try:
                if job["overdue_seconds"] > _STALE_AFTER_SECONDS:
                    marked = await loop.run_in_executor(None, _mark_stale_failed, job_id, job["overdue_seconds"])
                    if marked:
                        print(f"[publish_scheduler] {job_id} stale by {job['overdue_seconds']/86400:.1f}d — marked failed, not published", flush=True)
                    continue

                claimed = await loop.run_in_executor(None, _claim, job_id)
                if not claimed:
                    continue  # another worker already claimed it this tick
                print(f"[publish_scheduler] running {job_id}", flush=True)
                from job_runner import run_job  # noqa: PLC0415
                await loop.run_in_executor(None, run_job, job_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[publish_scheduler] job {job_id} failed: {exc}\n{traceback.format_exc()}", flush=True)


async def start(sb: Client) -> None:
    global _task, _sb
    _sb = sb
    if _task and not _task.done():
        print("[publish_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print("[publish_scheduler] task created", flush=True)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[publish_scheduler] stopped", flush=True)

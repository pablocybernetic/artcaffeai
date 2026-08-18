"""
scheduler_lock.py
------------------
Cross-worker lock for in-process asyncio schedulers (master_scheduler,
reminder_scheduler, meta_sync_scheduler). uvicorn runs multiple worker
processes, each an independent copy of the FastAPI app — including its
lifespan — so every scheduler's asyncio loop starts once per worker
and fires on the same wall-clock schedule. This gives each worker a
race-free way to ask "is it actually my turn to run?" via a single
atomic UPDATE ... WHERE, backed by the scheduler_locks table
(migration 021) — Postgres serializes concurrent updates to the same
row, so only one of N simultaneous claim attempts can ever see
locked_until in the past and successfully move it into the future.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from supabase import Client

_EPOCH_ISO = "1970-01-01T00:00:00+00:00"


def try_claim_run(sb: Client, name: str, hold_seconds: int) -> bool:
    """Returns True if this call won the race to run `name` right now —
    the caller should proceed. Returns False if another worker already
    claimed this run — the caller should skip it this cycle."""
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=hold_seconds)

    # Ensure a row exists (no-op if another worker already created it).
    try:
        sb.table("scheduler_locks").insert({"name": name, "locked_until": _EPOCH_ISO}).execute()
    except Exception:  # noqa: BLE001
        pass  # already exists — expected on every run after the first

    res = (
        sb.table("scheduler_locks")
        .update({"locked_until": until.isoformat()})
        .eq("name", name)
        .lt("locked_until", now.isoformat())
        .execute()
    )
    return bool(res.data)

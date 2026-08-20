"""
data_snapshot_scheduler.py
----------------------------
Background asyncio cron that pulls BigQuery-backed platform data (GA4 +
paid ads/transactions) for every concept once per day, at a configured
Kenyan-time hour — no manual "Refresh Data" click needed on the Chat with
Data page anymore.

Mirrors meta_sync_scheduler.py's pattern exactly, just daily instead of
hourly (single run_hour_eat instead of a start/end window) — module-level
_state, app_settings persistence, scheduler_lock for cross-worker safety.

Reuses data_routes._snapshot_concept() — the exact function the manual
"Refresh Data" button's POST /data/snapshot endpoint already calls per
concept — so this cron produces identical results to a manual click.

Environment variables:
  DATA_SNAPSHOT_ENABLED         Set to "false" to disable on startup (default: true)
  DATA_SNAPSHOT_RUN_HOUR_EAT    Hour of the day to run, EAT (default: 5)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

EAT = timezone(timedelta(hours=3))

_ENABLED_DEFAULT = os.environ.get("DATA_SNAPSHOT_ENABLED", "true").lower() != "false"
_RUN_HOUR_DEFAULT = int(os.environ.get("DATA_SNAPSHOT_RUN_HOUR_EAT", "5"))

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "run_hour_eat": _RUN_HOUR_DEFAULT,
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_result": None,
    "last_error": None,
}

_task: Optional[asyncio.Task] = None
_sb: Optional[Client] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict:
    # uvicorn runs multiple worker processes, each with its own in-memory
    # _state — a change made via a request handled by worker A never
    # updates worker B's copy on its own. Re-reading app_settings here
    # means a GET always reflects the true persisted value, whichever
    # worker answers it.
    _load_persisted()
    return dict(_state)


def set_enabled(enabled: bool) -> None:
    _state["enabled"] = bool(enabled)
    _persist()
    print(f"[data_snapshot_scheduler] enabled → {_state['enabled']}", flush=True)


def set_run_hour(run_hour_eat: int) -> None:
    _state["run_hour_eat"] = max(0, min(23, int(run_hour_eat)))
    _persist()
    print(f"[data_snapshot_scheduler] run hour updated → {_state['run_hour_eat']}:00 EAT", flush=True)


# ---------------------------------------------------------------------------
# Persistence (survives restarts/redeploys — env vars only seed the very
# first default on a brand-new deploy)
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "data_snapshot_scheduler"


def _persist() -> None:
    if _sb is None:
        return
    from app_settings import set_setting  # noqa: PLC0415
    set_setting(_sb, _SETTINGS_KEY, {
        "enabled": _state["enabled"],
        "run_hour_eat": _state["run_hour_eat"],
    })


def _load_persisted() -> None:
    if _sb is None:
        return
    from app_settings import get_setting  # noqa: PLC0415
    try:
        saved = get_setting(_sb, _SETTINGS_KEY, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[data_snapshot_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "run_hour_eat" in saved:
        _state["run_hour_eat"] = int(saved["run_hour_eat"])


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _now_eat() -> datetime:
    return datetime.now(EAT)


def _seconds_until_next_run_hour() -> float:
    now = _now_eat()
    target = now.replace(hour=_state["run_hour_eat"], minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _snapshot_all_concepts_sync(sb: Client) -> dict:
    """Same per-concept BigQuery pull the manual Refresh Data button
    triggers (data_routes._snapshot_concept) — synchronous, offloaded to
    a thread pool by the async loop below."""
    from data_routes import _snapshot_concept, BQ_SERVICE_ACCOUNT_JSON  # noqa: PLC0415

    if not BQ_SERVICE_ACCOUNT_JSON:
        return {"skipped": True, "reason": "BQ_SERVICE_ACCOUNT_JSON not configured"}

    concepts_res = sb.table("concepts").select("id,name").execute()
    concepts = concepts_res.data or []

    results: list[dict] = []
    for c in concepts:
        try:
            results.append(_snapshot_concept(c["id"]))
        except Exception as e:  # noqa: BLE001
            results.append({"concept_id": c["id"], "name": c.get("name"), "error": str(e)[:200]})

    return {"results": results}


async def _scheduler_loop() -> None:
    print(
        f"[data_snapshot_scheduler] loop started — "
        f"run_hour={_state['run_hour_eat']}:00 EAT  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        sleep_s = _seconds_until_next_run_hour()
        _state["next_run_at"] = (_now_eat() + timedelta(seconds=sleep_s)).isoformat()
        await asyncio.sleep(sleep_s)

        # Pick up settings changes made via a request another worker handled
        # (each uvicorn worker has its own in-memory _state).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_persisted)

        if not _state["enabled"]:
            print("[data_snapshot_scheduler] skipping — disabled", flush=True)
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        loop = asyncio.get_event_loop()
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "data_snapshot_scheduler", 23 * 3600)
        if not claimed:
            print("[data_snapshot_scheduler] skipping — another worker already claimed this run", flush=True)
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now_eat().isoformat()
        _state["next_run_at"] = None
        _state["last_error"] = None

        print(f"[data_snapshot_scheduler] run #{run_num} starting", flush=True)
        try:
            result = await loop.run_in_executor(None, _snapshot_all_concepts_sync, _sb)
            _state["last_result"] = result
            print(f"[data_snapshot_scheduler] run #{run_num} done — {result}", flush=True)
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[data_snapshot_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(sb: Client) -> None:
    global _task, _sb
    _sb = sb
    _load_persisted()
    if _task and not _task.done():
        print("[data_snapshot_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(
        f"[data_snapshot_scheduler] task created — next run in "
        f"{_seconds_until_next_run_hour() / 60:.1f} min",
        flush=True,
    )


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[data_snapshot_scheduler] stopped", flush=True)

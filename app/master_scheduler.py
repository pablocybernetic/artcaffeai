"""
master_scheduler.py
-------------------
Background asyncio cron for the Master Agent.

Runs run_master_agent on a configurable interval inside the FastAPI process.
Started via the app lifespan in app.py.

Environment variables:
  MASTER_AGENT_INTERVAL_MINUTES   Cron interval in minutes (default: 30, min: 5)
  MASTER_AGENT_ENABLED            Set to "false" to disable on startup (default: true)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

_INTERVAL_DEFAULT = max(5, int(os.environ.get("MASTER_AGENT_INTERVAL_MINUTES", "30")))
_ENABLED_DEFAULT = os.environ.get("MASTER_AGENT_ENABLED", "true").lower() != "false"

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "interval_minutes": _INTERVAL_DEFAULT,
    "last_run_at": None,
    "next_run_at": None,
    "last_health": None,
    "run_count": 0,
    "last_error": None,
}

_task: Optional[asyncio.Task] = None
_sb = None  # Supabase client injected by start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict:
    return dict(_state)


def set_interval(minutes: int) -> None:
    """Change the cron interval. Minimum 5 min. Takes effect after the current sleep."""
    _state["interval_minutes"] = max(5, int(minutes))
    _persist()
    print(f"[master_scheduler] interval updated → {_state['interval_minutes']} min", flush=True)


def set_enabled(enabled: bool) -> None:
    _state["enabled"] = bool(enabled)
    _persist()
    print(f"[master_scheduler] enabled → {_state['enabled']}", flush=True)


# ---------------------------------------------------------------------------
# Persistence (survives restarts/redeploys — env vars only seed the very
# first default on a brand-new deploy)
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "master_scheduler"


def _persist() -> None:
    if _sb is None:
        return
    from app_settings import set_setting  # noqa: PLC0415
    set_setting(_sb, _SETTINGS_KEY, {
        "enabled": _state["enabled"],
        "interval_minutes": _state["interval_minutes"],
    })


def _load_persisted() -> None:
    if _sb is None:
        return
    from app_settings import get_setting  # noqa: PLC0415
    try:
        saved = get_setting(_sb, _SETTINGS_KEY, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[master_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "interval_minutes" in saved:
        _state["interval_minutes"] = max(5, int(saved["interval_minutes"]))


# ---------------------------------------------------------------------------
# Internal loop
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_once() -> dict:
    from anthropic import Anthropic
    from master_agent import run_master_agent

    anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    loop = asyncio.get_event_loop()
    # run_master_agent is synchronous — offload to thread pool
    result: dict = await loop.run_in_executor(None, run_master_agent, _sb, anthropic)
    return result


async def _scheduler_loop() -> None:
    print(
        f"[master_scheduler] loop started — "
        f"interval={_state['interval_minutes']}min  enabled={_state['enabled']}",
        flush=True,
    )

    while True:
        interval = _state["interval_minutes"]
        _state["next_run_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=interval)
        ).isoformat()

        await asyncio.sleep(interval * 60)

        if not _state["enabled"]:
            print("[master_scheduler] skipping — disabled", flush=True)
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        loop = asyncio.get_event_loop()
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "master_scheduler", interval * 60 - 5)
        if not claimed:
            print("[master_scheduler] skipping — another worker already claimed this run", flush=True)
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now_iso()
        _state["next_run_at"] = None
        _state["last_error"] = None

        print(f"[master_scheduler] run #{run_num} starting", flush=True)
        try:
            result = await _run_once()
            _state["last_health"] = result.get("health")
            print(
                f"[master_scheduler] run #{run_num} done — health={_state['last_health']}",
                flush=True,
            )
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[master_scheduler] run #{run_num} failed: {err_str}\n"
                f"{traceback.format_exc()}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(sb) -> None:
    global _task, _sb
    _sb = sb
    _load_persisted()
    if _task and not _task.done():
        print("[master_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(
        f"[master_scheduler] task created — "
        f"first run in {_state['interval_minutes']} min",
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
    print("[master_scheduler] stopped", flush=True)

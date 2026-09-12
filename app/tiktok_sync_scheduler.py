"""
tiktok_sync_scheduler.py
--------------------------
Background asyncio cron that auto-syncs TikTok organic data for every
concept/brand with connected credentials, once per hour during a
configured Kenyan-time window — mirrors meta_sync_scheduler.py exactly
(same window/enabled settings shape, same claim-lock pattern), so the
two platforms behave identically from an admin's point of view.

A concept with no (or expired-beyond-refresh) TikTok credentials is
skipped, not fatal — sync_tiktok_organic() itself handles token refresh
transparently; this scheduler only needs to not crash if that raises.

Environment variables:
  TIKTOK_SYNC_ENABLED           Set to "false" to disable on startup (default: true)
  TIKTOK_SYNC_START_HOUR_EAT    First hour of the day to sync, EAT, inclusive (default: 6)
  TIKTOK_SYNC_END_HOUR_EAT      Last hour of the day to sync, EAT, inclusive (default: 17)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

EAT = timezone(timedelta(hours=3))

_ENABLED_DEFAULT = os.environ.get("TIKTOK_SYNC_ENABLED", "true").lower() != "false"
_START_HOUR_DEFAULT = int(os.environ.get("TIKTOK_SYNC_START_HOUR_EAT", "6"))
_END_HOUR_DEFAULT = int(os.environ.get("TIKTOK_SYNC_END_HOUR_EAT", "17"))

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "start_hour_eat": _START_HOUR_DEFAULT,
    "end_hour_eat": _END_HOUR_DEFAULT,
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_result": None,
    "last_error": None,
}

_task: Optional[asyncio.Task] = None
_sb: Optional[Client] = None
_SETTINGS_KEY = "tiktok_sync_scheduler"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict:
    _load_persisted()
    return dict(_state)


def set_enabled(enabled: bool) -> None:
    _state["enabled"] = bool(enabled)
    _persist()
    print(f"[tiktok_sync_scheduler] enabled → {_state['enabled']}", flush=True)


def set_window(start_hour_eat: int, end_hour_eat: int) -> None:
    _state["start_hour_eat"] = max(0, min(23, int(start_hour_eat)))
    _state["end_hour_eat"] = max(0, min(23, int(end_hour_eat)))
    _persist()
    print(
        f"[tiktok_sync_scheduler] window updated → "
        f"{_state['start_hour_eat']}:00-{_state['end_hour_eat']}:00 EAT",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist() -> None:
    if _sb is None:
        return
    from app_settings import set_setting  # noqa: PLC0415
    set_setting(_sb, _SETTINGS_KEY, {
        "enabled": _state["enabled"],
        "start_hour_eat": _state["start_hour_eat"],
        "end_hour_eat": _state["end_hour_eat"],
    })


def _load_persisted() -> None:
    if _sb is None:
        return
    from app_settings import get_setting  # noqa: PLC0415
    try:
        saved = get_setting(_sb, _SETTINGS_KEY, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[tiktok_sync_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "start_hour_eat" in saved:
        _state["start_hour_eat"] = int(saved["start_hour_eat"])
    if "end_hour_eat" in saved:
        _state["end_hour_eat"] = int(saved["end_hour_eat"])


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _now_eat() -> datetime:
    return datetime.now(EAT)


def _seconds_until_next_hour() -> float:
    now = _now_eat()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max(1.0, (next_hour - now).total_seconds())


def _sync_all_concepts_sync(sb: Client) -> dict:
    from connectors.tiktok_organic_connector import sync_tiktok_organic  # noqa: PLC0415

    concepts_res = sb.table("concepts").select("id,name").execute()
    concepts = concepts_res.data or []

    results: list[dict] = []
    for c in concepts:
        concept_id = c["id"]
        name = c.get("name") or concept_id
        try:
            summary = sync_tiktok_organic(sb=sb, concept_id=concept_id, date_range_days=28)
            if summary.get("skipped"):
                results.append({"concept": name, "skipped": True, "reason": summary.get("reason")})
                continue
            followers = summary.get("totals", {}).get("followers", 0)
            results.append({"concept": name, "ok": True, "followers": followers})
        except Exception as e:  # noqa: BLE001
            results.append({"concept": name, "ok": False, "error": str(e)[:200]})

    return {"results": results}


async def _scheduler_loop() -> None:
    print(
        f"[tiktok_sync_scheduler] loop started — "
        f"window={_state['start_hour_eat']}:00-{_state['end_hour_eat']}:00 EAT  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        sleep_s = _seconds_until_next_hour()
        _state["next_run_at"] = (_now_eat() + timedelta(seconds=sleep_s)).isoformat()
        await asyncio.sleep(sleep_s)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_persisted)

        current_hour = _now_eat().hour
        if not _state["enabled"]:
            print("[tiktok_sync_scheduler] skipping — disabled", flush=True)
            continue
        if not (_state["start_hour_eat"] <= current_hour <= _state["end_hour_eat"]):
            print(f"[tiktok_sync_scheduler] skipping — hour={current_hour} EAT outside window", flush=True)
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "tiktok_sync_scheduler", 45 * 60)
        if not claimed:
            print("[tiktok_sync_scheduler] skipping — another worker already claimed this run", flush=True)
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now_eat().isoformat()
        _state["next_run_at"] = None
        _state["last_error"] = None

        print(f"[tiktok_sync_scheduler] run #{run_num} starting (hour={current_hour} EAT)", flush=True)
        try:
            result = await loop.run_in_executor(None, _sync_all_concepts_sync, _sb)
            _state["last_result"] = result
            print(f"[tiktok_sync_scheduler] run #{run_num} done — {result}", flush=True)
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[tiktok_sync_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
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
        print("[tiktok_sync_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(
        f"[tiktok_sync_scheduler] task created — next aligned run in "
        f"{_seconds_until_next_hour() / 60:.1f} min",
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
    print("[tiktok_sync_scheduler] stopped", flush=True)

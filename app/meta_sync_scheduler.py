"""
meta_sync_scheduler.py
------------------------
Background asyncio cron that auto-syncs Meta (Instagram + Facebook)
organic data for every concept, once per hour, only during a
configured Kenyan-time window — no manual "Sync from Meta" click
needed on the Dashboard anymore.

Kenya is UTC+3 year-round (no DST), so the window is computed with a
fixed offset rather than depending on a timezone database. Mirrors
master_scheduler.py's start()/stop() pattern — started via the app
lifespan in app.py.

A concept with no (or broken) Meta credentials is skipped, not
fatal — matches how the manual sync endpoint already degrades
(FB-only or IG-only concepts still sync whichever platform they have).

Environment variables:
  META_SYNC_ENABLED           Set to "false" to disable on startup (default: true)
  META_SYNC_START_HOUR_EAT    First hour of the day to sync, EAT, inclusive (default: 6)
  META_SYNC_END_HOUR_EAT      Last hour of the day to sync, EAT, inclusive (default: 17)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

EAT = timezone(timedelta(hours=3))

_ENABLED_DEFAULT = os.environ.get("META_SYNC_ENABLED", "true").lower() != "false"
_START_HOUR_DEFAULT = int(os.environ.get("META_SYNC_START_HOUR_EAT", "6"))
_END_HOUR_DEFAULT = int(os.environ.get("META_SYNC_END_HOUR_EAT", "17"))

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict:
    return dict(_state)


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
    """Sync every concept with active Meta credentials. Synchronous —
    offloaded to a thread pool by the async loop below, same as
    master_scheduler.py does for run_master_agent."""
    from connectors.meta_organic_connector import sync_meta_organic  # noqa: PLC0415

    concepts_res = sb.table("concepts").select("id,name").execute()
    concepts = concepts_res.data or []

    results: list[dict] = []
    for c in concepts:
        concept_id = c["id"]
        name = c.get("name") or concept_id
        try:
            creds_res = (
                sb.table("platform_credentials")
                .select("*")
                .eq("platform", "meta")
                .eq("concept_id", concept_id)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            creds = (creds_res.data or [{}])[0] if creds_res.data else {}
            access_token = creds.get("access_token")
            instagram_account_id = creds.get("ig_user_id") or creds.get("instagram_account_id") or ""
            page_id = creds.get("page_id") or ""

            if not access_token or (not instagram_account_id and not page_id):
                results.append({"concept": name, "skipped": True, "reason": "no Meta credentials configured"})
                continue

            summary = sync_meta_organic(
                sb=sb,
                concept_id=concept_id,
                access_token=access_token,
                instagram_account_id=instagram_account_id,
                page_id=page_id,
                date_range_days=28,
            )
            followers = summary.get("instagram", {}).get("totals", {}).get("followers", 0)
            results.append({"concept": name, "ok": True, "followers": followers})
        except Exception as e:  # noqa: BLE001
            results.append({"concept": name, "ok": False, "error": str(e)[:200]})

    return {"results": results}


async def _scheduler_loop() -> None:
    print(
        f"[meta_sync_scheduler] loop started — "
        f"window={_state['start_hour_eat']}:00-{_state['end_hour_eat']}:00 EAT  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        sleep_s = _seconds_until_next_hour()
        _state["next_run_at"] = (_now_eat() + timedelta(seconds=sleep_s)).isoformat()
        await asyncio.sleep(sleep_s)

        current_hour = _now_eat().hour
        if not _state["enabled"]:
            print("[meta_sync_scheduler] skipping — disabled", flush=True)
            continue
        if not (_state["start_hour_eat"] <= current_hour <= _state["end_hour_eat"]):
            print(f"[meta_sync_scheduler] skipping — hour={current_hour} EAT outside window", flush=True)
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now_eat().isoformat()
        _state["next_run_at"] = None
        _state["last_error"] = None

        print(f"[meta_sync_scheduler] run #{run_num} starting (hour={current_hour} EAT)", flush=True)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _sync_all_concepts_sync, _sb)
            _state["last_result"] = result
            print(f"[meta_sync_scheduler] run #{run_num} done — {result}", flush=True)
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[meta_sync_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(sb: Client) -> None:
    global _task, _sb
    _sb = sb
    if _task and not _task.done():
        print("[meta_sync_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(
        f"[meta_sync_scheduler] task created — next aligned run in "
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
    print("[meta_sync_scheduler] stopped", flush=True)

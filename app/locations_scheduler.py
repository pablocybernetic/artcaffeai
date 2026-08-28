"""
locations_scheduler.py
------------------------
Background asyncio cron that syncs Google Places data for every active
location — no manual "Sync Now" click needed. Mirrors the other four
schedulers' shape (master_scheduler.py, reminder_scheduler.py,
meta_sync_scheduler.py, data_snapshot_scheduler.py) exactly.

Unlike those, the admin-configured cadence isn't "every N minutes" — it's
one of hourly/6_hours/12_hours/daily/manual (spec §13: "cron executes
every hour, backend checks last_global_google_sync, skip if not due
yet"). So this loop ticks frequently (every 15 min) but only actually
runs sync_all_locations() once the configured interval has genuinely
elapsed since last_run_at — letting the sync cadence be changed from
Admin Settings without ever touching the VM.

Settings (enabled, sync_frequency, encrypted Google credentials,
per-field sync toggles) all live under one app_settings key,
"locations_google_integration", shared with locations_routes.py's
settings endpoints.

Environment variables:
  LOCATIONS_SYNC_ENABLED   Set to "false" to disable on startup (default: true)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

_ENABLED_DEFAULT = os.environ.get("LOCATIONS_SYNC_ENABLED", "true").lower() != "false"
_TICK_SECONDS = 15 * 60  # how often the loop wakes to check whether a sync is due

_FREQUENCY_SECONDS = {
    "hourly": 3600,
    "6_hours": 6 * 3600,
    "12_hours": 12 * 3600,
    "daily": 24 * 3600,
    "manual": None,  # never auto-runs
}

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "sync_frequency": "6_hours",
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_result": None,
    "last_error": None,
}

_task: Optional[asyncio.Task] = None
_sb: Optional[Client] = None

SETTINGS_KEY = "locations_google_integration"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict:
    # uvicorn runs multiple worker processes, each with its own in-memory
    # _state — re-reading app_settings here means a GET always reflects
    # the true persisted value, whichever worker answers it.
    _load_persisted()
    return dict(_state)


def set_enabled(enabled: bool) -> None:
    _state["enabled"] = bool(enabled)
    _persist()
    print(f"[locations_scheduler] enabled → {_state['enabled']}", flush=True)


def set_sync_frequency(frequency: str) -> None:
    if frequency not in _FREQUENCY_SECONDS:
        raise ValueError(f"Invalid sync_frequency: {frequency}")
    _state["sync_frequency"] = frequency
    _persist()
    print(f"[locations_scheduler] sync_frequency → {frequency}", flush=True)


# ---------------------------------------------------------------------------
# Persistence — shared settings blob with locations_routes.py (credentials,
# per-field toggles live in the same app_settings key, encrypted where
# sensitive; this module only ever touches its own enabled/sync_frequency
# keys, never overwriting the credential fields another worker/request set).
# ---------------------------------------------------------------------------

def _persist() -> None:
    if _sb is None:
        return
    from app_settings import get_setting, set_setting  # noqa: PLC0415
    existing = get_setting(_sb, SETTINGS_KEY, {}) or {}
    existing["enabled"] = _state["enabled"]
    existing["sync_frequency"] = _state["sync_frequency"]
    set_setting(_sb, SETTINGS_KEY, existing)


def _load_persisted() -> None:
    if _sb is None:
        return
    from app_settings import get_setting  # noqa: PLC0415
    try:
        saved = get_setting(_sb, SETTINGS_KEY, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[locations_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "sync_frequency" in saved and saved["sync_frequency"] in _FREQUENCY_SECONDS:
        _state["sync_frequency"] = saved["sync_frequency"]


def _get_places_api_key() -> Optional[str]:
    if _sb is None:
        return None
    from app_settings import get_setting  # noqa: PLC0415
    from secrets_crypto import decrypt_value  # noqa: PLC0415
    saved = get_setting(_sb, SETTINGS_KEY, {}) or {}
    enc = saved.get("places_api_key_enc")
    if not enc:
        return None
    try:
        return decrypt_value(enc)
    except Exception as exc:  # noqa: BLE001
        print(f"[locations_scheduler] could not decrypt places_api_key: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_due() -> bool:
    freq = _state["sync_frequency"]
    interval = _FREQUENCY_SECONDS.get(freq)
    if interval is None:  # manual
        return False
    if not _state["last_run_at"]:
        return True
    last = datetime.fromisoformat(_state["last_run_at"].replace("Z", "+00:00"))
    return (_now() - last).total_seconds() >= interval


def _sync_sync(sb: Client) -> dict:
    from app_settings import get_setting  # noqa: PLC0415
    from google_location_sync_service import sync_all_locations  # noqa: PLC0415

    api_key = _get_places_api_key()
    if not api_key:
        return {"skipped": True, "reason": "Google Places API key not configured"}

    saved = get_setting(sb, SETTINGS_KEY, {}) or {}
    sync_fields = saved.get("sync_fields")
    return sync_all_locations(sb, api_key, sync_fields=sync_fields)


async def _scheduler_loop() -> None:
    print(
        f"[locations_scheduler] loop started — "
        f"frequency={_state['sync_frequency']}  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        await asyncio.sleep(_TICK_SECONDS)

        # Pick up settings changes made via a request another worker handled
        # (each uvicorn worker has its own in-memory _state).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_persisted)

        if not _state["enabled"]:
            continue
        if not _is_due():
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        interval = _FREQUENCY_SECONDS.get(_state["sync_frequency"]) or 3600
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "locations_scheduler", max(300, interval - 60))
        if not claimed:
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now().isoformat()
        _state["last_error"] = None

        print(f"[locations_scheduler] run #{run_num} starting", flush=True)
        try:
            result = await loop.run_in_executor(None, _sync_sync, _sb)
            _state["last_result"] = result
            print(f"[locations_scheduler] run #{run_num} done — {result}", flush=True)
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[locations_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
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
        print("[locations_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print("[locations_scheduler] task created", flush=True)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[locations_scheduler] stopped", flush=True)

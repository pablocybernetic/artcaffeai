"""
ads_status_scheduler.py
-------------------------
Background asyncio cron that pulls live status + spend back from Meta
for every ad_campaigns row we've pushed to the platform — mirrors
locations_scheduler.py's shape exactly (module-level _state dict,
app_settings-backed persistence, scheduler_lock.try_claim_run for
cross-worker safety since uvicorn runs multiple workers).

ad_campaigns/ad_sets/ads are a local mirror of Meta's own objects (same
pattern as the Locations module mirrors Google Places): our UI pushes
creates/status/budget changes to Meta, and this scheduler periodically
pulls live status/spend back so the dashboard reflects e.g. a pause
flipped from Meta Ads Manager directly, not just from our own UI.

A campaign with no (or broken) Meta credentials is skipped, not fatal,
and each campaign's sync is wrapped in its own try/except so one bad
campaign never kills the rest of the batch (matches
meta_sync_scheduler.py's per-concept try/except pattern).

Environment variables:
  ADS_STATUS_SYNC_ENABLED  Set to "false" to disable on startup (default: true)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

_ENABLED_DEFAULT = os.environ.get("ADS_STATUS_SYNC_ENABLED", "true").lower() != "false"
_TICK_SECONDS = 5 * 60  # how often the loop wakes to check whether a sync is due

_STATUS_MAP = {
    "ACTIVE": "active",
    "PAUSED": "paused",
    "ARCHIVED": "archived",
    "DELETED": "archived",
}

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "interval_minutes": 30,
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_result": None,
    "last_error": None,
}

_task: Optional[asyncio.Task] = None
_sb: Optional[Client] = None

_SETTINGS_KEY = "ads_status_scheduler"


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
    print(f"[ads_status_scheduler] enabled → {_state['enabled']}", flush=True)


def set_interval_minutes(interval_minutes: int) -> None:
    _state["interval_minutes"] = max(5, int(interval_minutes))
    _persist()
    print(f"[ads_status_scheduler] interval_minutes → {_state['interval_minutes']}", flush=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

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
        print(f"[ads_status_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "interval_minutes" in saved:
        _state["interval_minutes"] = int(saved["interval_minutes"])


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_due() -> bool:
    if not _state["last_run_at"]:
        return True
    last = datetime.fromisoformat(_state["last_run_at"].replace("Z", "+00:00"))
    return (_now() - last).total_seconds() >= _state["interval_minutes"] * 60


def _map_status(meta_status: Optional[str]) -> Optional[str]:
    return _STATUS_MAP.get((meta_status or "").upper())


def _sync_campaigns_sync(sb: Client) -> dict:
    from connectors.meta_ads_management_connector import fetch_campaign_status  # noqa: PLC0415

    campaigns_res = (
        sb.table("ad_campaigns")
        .select("id,concept_id,platform_campaign_id,platform")
        .not_.is_("platform_campaign_id", "null")
        .execute()
    )
    campaigns = [c for c in (campaigns_res.data or []) if c.get("platform") == "meta"]

    # Cache credentials per concept — several campaigns typically share one
    # concept's Meta account, no need to re-query per campaign.
    creds_cache: dict[Optional[str], Optional[dict]] = {}
    results: list[dict] = []

    for camp in campaigns:
        campaign_id = camp["id"]
        platform_campaign_id = camp.get("platform_campaign_id")
        concept_id = camp.get("concept_id")
        try:
            if concept_id not in creds_cache:
                creds_res = (
                    sb.table("platform_credentials")
                    .select("access_token")
                    .eq("platform", "meta")
                    .eq("concept_id", concept_id)
                    .eq("is_active", True)
                    .limit(1)
                    .execute()
                )
                creds_cache[concept_id] = (creds_res.data or [None])[0]
            access_token = (creds_cache.get(concept_id) or {}).get("access_token")
            if not access_token:
                results.append({"campaign_id": campaign_id, "skipped": True, "reason": "no Meta credentials configured"})
                continue

            live = fetch_campaign_status(access_token, platform_campaign_id)
            update: dict = {"last_synced_at": _now().isoformat(), "last_sync_error": None}
            mapped_status = _map_status(live.get("status"))
            if mapped_status:
                update["status"] = mapped_status
            if live.get("spend") is not None:
                update["spend"] = live["spend"]
            sb.table("ad_campaigns").update(update).eq("id", campaign_id).execute()
            results.append({
                "campaign_id": campaign_id,
                "ok": True,
                "status": mapped_status,
                "spend": live.get("spend"),
            })
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            try:
                sb.table("ad_campaigns").update({"last_sync_error": err}).eq("id", campaign_id).execute()
            except Exception:  # noqa: BLE001
                pass
            results.append({"campaign_id": campaign_id, "ok": False, "error": err})

    return {"results": results}


async def _scheduler_loop() -> None:
    print(
        f"[ads_status_scheduler] loop started — "
        f"interval={_state['interval_minutes']}min  enabled={_state['enabled']}",
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
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "ads_status_scheduler", 25 * 60)
        if not claimed:
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now().isoformat()
        _state["last_error"] = None

        print(f"[ads_status_scheduler] run #{run_num} starting", flush=True)
        try:
            result = await loop.run_in_executor(None, _sync_campaigns_sync, _sb)
            _state["last_result"] = result
            print(f"[ads_status_scheduler] run #{run_num} done — {result}", flush=True)
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[ads_status_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
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
        print("[ads_status_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print("[ads_status_scheduler] task created", flush=True)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[ads_status_scheduler] stopped", flush=True)

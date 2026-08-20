"""
reminder_scheduler.py
----------------------
Background asyncio cron that reminds team members about their own
upcoming scheduled posts, ahead of each person's own configured lead
time (team_members.notification_preferences.reminder_lead_minutes,
default 60). Mirrors master_scheduler.py's start()/stop() pattern —
started via the app lifespan in app.py.

Scheduled posts live in two places (same dual-source split the
Calendar UI uses): calendar_entries when one exists for a content
item, otherwise content_items.scheduled_at directly. A post only gets
reminded once per recipient — checked via a notifications row with
type="post_reminder" for that (recipient, content_item) pair, not a DB
constraint, since this loop runs as a single process with no
concurrent writers for the same pair.

Environment variables:
  REMINDER_POLL_INTERVAL_MINUTES   Poll interval in minutes (default: 5, min: 1)
  REMINDER_ENABLED                 Set to "false" to disable on startup (default: true)
  REMINDER_HORIZON_MINUTES         How far ahead to look for scheduled posts (default: 1440 = 24h)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

from notification_service import notify_team, _eligible_team_members, DASHBOARD_URL, _platform_label

_INTERVAL_DEFAULT = max(1, int(os.environ.get("REMINDER_POLL_INTERVAL_MINUTES", "5")))
_ENABLED_DEFAULT = os.environ.get("REMINDER_ENABLED", "true").lower() != "false"
_HORIZON_DEFAULT = max(60, int(os.environ.get("REMINDER_HORIZON_MINUTES", "1440")))
_DEFAULT_LEAD_MINUTES = 60

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "interval_minutes": _INTERVAL_DEFAULT,
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_reminders_sent": 0,
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
    print(f"[reminder_scheduler] enabled → {_state['enabled']}", flush=True)


def set_interval(minutes: int) -> None:
    _state["interval_minutes"] = max(1, int(minutes))
    _persist()
    print(f"[reminder_scheduler] interval updated → {_state['interval_minutes']} min", flush=True)


# ---------------------------------------------------------------------------
# Persistence (survives restarts/redeploys — env vars only seed the very
# first default on a brand-new deploy)
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "reminder_scheduler"


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
        print(f"[reminder_scheduler] failed to load persisted settings: {exc}", flush=True)
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


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _get_upcoming_scheduled_items(sb: Client, horizon_minutes: int) -> list[dict]:
    """Posts scheduled within the next horizon_minutes, across both
    calendar_entries and content_items (dual-source, mirroring the
    Calendar UI), future only, excluding already-published ones. Skips
    calendar_entries with no content_item_id — reminder dedup is keyed
    on content_item_id, so those can't be tracked (rare in practice)."""
    now_iso = _now().isoformat()
    horizon_iso = (_now() + timedelta(minutes=horizon_minutes)).isoformat()

    ce_res = (
        sb.table("calendar_entries")
        .select("id,content_item_id,platform,scheduled_at,content_status")
        .gte("scheduled_at", now_iso)
        .lte("scheduled_at", horizon_iso)
        .neq("content_status", "published")
        .execute()
    )
    calendar_entries = ce_res.data or []

    ci_res = (
        sb.table("content_items")
        .select("id,platform,status,scheduled_at,headline,title")
        .not_.is_("scheduled_at", "null")
        .gte("scheduled_at", now_iso)
        .lte("scheduled_at", horizon_iso)
        .neq("status", "rejected")
        .execute()
    )
    content_items = ci_res.data or []
    covered = {ce["content_item_id"] for ce in calendar_entries if ce.get("content_item_id")}

    ce_item_ids = [ce["content_item_id"] for ce in calendar_entries if ce.get("content_item_id")]
    titles_by_item: dict[str, str] = {}
    if ce_item_ids:
        t_res = sb.table("content_items").select("id,headline,title").in_("id", ce_item_ids).execute()
        titles_by_item = {
            r["id"]: (r.get("headline") or r.get("title") or "Untitled post")
            for r in (t_res.data or [])
        }

    items: list[dict] = []
    for ce in calendar_entries:
        cid = ce.get("content_item_id")
        if not cid:
            continue
        items.append({
            "content_item_id": cid,
            "platform": ce.get("platform"),
            "scheduled_at": ce["scheduled_at"],
            "title": titles_by_item.get(cid, "Untitled post"),
        })

    for ci in content_items:
        if ci["id"] in covered:
            continue
        items.append({
            "content_item_id": ci["id"],
            "platform": ci.get("platform"),
            "scheduled_at": ci["scheduled_at"],
            "title": ci.get("headline") or ci.get("title") or "Untitled post",
        })
    return items


def _already_reminded(sb: Client, *, recipient_id: str, content_item_id: str) -> bool:
    res = (
        sb.table("notifications")
        .select("id")
        .eq("recipient_id", recipient_id)
        .eq("content_item_id", content_item_id)
        .eq("type", "post_reminder")
        .limit(1)
        .execute()
    )
    return bool(res.data)


def _run_once_sync(sb: Client) -> dict:
    items = _get_upcoming_scheduled_items(sb, _HORIZON_DEFAULT)
    if not items:
        return {"checked": 0, "sent": 0}

    members = _eligible_team_members(sb, notif_type="reminders", role_slugs=["admin", "content_manager"])
    now = _now()
    sent = 0

    for member in members:
        prefs = member.get("notification_preferences") or {}
        lead_minutes = prefs.get("reminder_lead_minutes")
        try:
            lead_minutes = int(lead_minutes) if lead_minutes is not None else _DEFAULT_LEAD_MINUTES
        except (TypeError, ValueError):
            lead_minutes = _DEFAULT_LEAD_MINUTES

        for item in items:
            scheduled_at = _parse_ts(item["scheduled_at"])
            minutes_until = (scheduled_at - now).total_seconds() / 60
            if not (0 <= minutes_until <= lead_minutes):
                continue
            if _already_reminded(sb, recipient_id=member["id"], content_item_id=item["content_item_id"]):
                continue

            when_fmt = scheduled_at.strftime("%A, %d %B %Y at %H:%M UTC")
            platform_label = _platform_label(item.get("platform") or "", {})
            subject = f'Artcaffe — Reminder: "{item["title"]}" publishes soon'
            html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe AI Marketing</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">Hi {member.get('full_name', 'there')},</p>
    <p style="font-size:15px;color:#374151;">
      This is a reminder that your post is going out soon.
    </p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;">
      <p style="margin:0 0 8px;font-weight:700;font-size:14px;color:#1a1a1a;">"{item['title']}"</p>
      <p style="margin:4px 0;font-size:13px;color:#6b7280;"><strong>Publishing:</strong> {when_fmt}</p>
      <p style="margin:4px 0;font-size:13px;color:#6b7280;"><strong>Platform:</strong> {platform_label}</p>
    </div>
    <a href="{DASHBOARD_URL}/calendar"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      View in Calendar
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
            ok = notify_team(
                sb,
                recipient_id=member["id"],
                recipient_email=member["email"],
                recipient_name=member.get("full_name") or "",
                subject=subject,
                html=html,
                content_item_id=item["content_item_id"],
                type="post_reminder",
                payload={"content_item_id": item["content_item_id"], "title": item["title"], "minutes_until": minutes_until},
            )
            if ok:
                sent += 1

    return {"checked": len(items), "sent": sent}


async def _scheduler_loop() -> None:
    print(
        f"[reminder_scheduler] loop started — "
        f"interval={_state['interval_minutes']}min  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        interval = _state["interval_minutes"]
        _state["next_run_at"] = (_now() + timedelta(minutes=interval)).isoformat()
        await asyncio.sleep(interval * 60)

        # Pick up settings changes made via a request another worker handled
        # (each uvicorn worker has its own in-memory _state).
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_persisted)

        if not _state["enabled"]:
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        claimed = await loop.run_in_executor(None, try_claim_run, _sb, "reminder_scheduler", interval * 60 - 5)
        if not claimed:
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now().isoformat()
        _state["next_run_at"] = None
        _state["last_error"] = None

        try:
            result = await loop.run_in_executor(None, _run_once_sync, _sb)
            _state["last_reminders_sent"] = result.get("sent", 0)
            if result.get("sent"):
                print(
                    f"[reminder_scheduler] run #{run_num} — checked={result.get('checked')} "
                    f"sent={result.get('sent')}",
                    flush=True,
                )
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[reminder_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
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
        print("[reminder_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(f"[reminder_scheduler] task created — first run in {_state['interval_minutes']} min", flush=True)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[reminder_scheduler] stopped", flush=True)

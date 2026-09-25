"""
table_booking_reminder_scheduler.py
--------------------------------------
Background asyncio cron that reminds customers about their own upcoming
confirmed table booking, a configurable number of hours ahead of the
reservation. Structurally mirrors reminder_scheduler.py's start()/stop()
and state/settings pattern (own app_settings key, own scheduler_locks
row for cross-worker safety) — started via the app lifespan in app.py.

booking_date/booking_time carry no timezone of their own (a customer
picks them in a plain HTML date/time input while dining locally), so
— like every other EAT-anchored scheduler in this codebase
(meta_sync_scheduler, data_snapshot_scheduler, tiktok_sync_scheduler)
— they're treated as Africa/Nairobi wall-clock time via the same fixed
UTC+3 offset (Kenya has no DST).

A reminder is attempted at most once per booking: reminder_sent_at is
set after the attempt regardless of whether email/SMS actually
succeeded, so a channel failure never causes repeated retries hammering
Resend/Onfon every poll cycle. Each channel's own error is recorded
independently (reminder_email_error / reminder_sms_error) so the admin
Table Bookings report shows exactly what did/didn't go out — same
error-handling shape as the original booking-confirmation notifications
in table_booking_routes.py.

Environment variables:
  TABLE_BOOKING_REMINDER_POLL_INTERVAL_MINUTES   Poll interval in minutes (default: 10, min: 1)
  TABLE_BOOKING_REMINDER_ENABLED                 Set to "false" to disable on startup (default: true)
  TABLE_BOOKING_REMINDER_HOURS_BEFORE            How many hours ahead of the reservation to remind (default: 3)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

import app_settings
import ics_helper
import notification_service
from connectors import onfon_sms_connector
from locations_public_routes import _directions_url
from secrets_crypto import decrypt_value

EAT = timezone(timedelta(hours=3))

_INTERVAL_DEFAULT = max(1, int(os.environ.get("TABLE_BOOKING_REMINDER_POLL_INTERVAL_MINUTES", "10")))
_ENABLED_DEFAULT = os.environ.get("TABLE_BOOKING_REMINDER_ENABLED", "true").lower() != "false"
_HOURS_BEFORE_DEFAULT = max(1, int(os.environ.get("TABLE_BOOKING_REMINDER_HOURS_BEFORE", "3")))

# Same app_settings key table_booking_routes.py's SMS credentials live
# under — duplicated locally (client_id/access_key/etc. lookup) rather
# than imported, matching this codebase's own convention of not sharing
# route-module internals across files (see locations_routes.py's header).
_SMS_SETTINGS_KEY = "sms_provider_credentials"

_state: dict = {
    "enabled": _ENABLED_DEFAULT,
    "interval_minutes": _INTERVAL_DEFAULT,
    "hours_before": _HOURS_BEFORE_DEFAULT,
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
    # _state — re-reading app_settings here means a GET always reflects
    # the true persisted value, whichever worker answers it.
    _load_persisted()
    return dict(_state)


def set_enabled(enabled: bool) -> None:
    _state["enabled"] = bool(enabled)
    _persist()
    print(f"[table_booking_reminder_scheduler] enabled → {_state['enabled']}", flush=True)


def set_interval(minutes: int) -> None:
    _state["interval_minutes"] = max(1, int(minutes))
    _persist()
    print(f"[table_booking_reminder_scheduler] interval updated → {_state['interval_minutes']} min", flush=True)


def set_hours_before(hours: int) -> None:
    _state["hours_before"] = max(1, int(hours))
    _persist()
    print(f"[table_booking_reminder_scheduler] hours_before updated → {_state['hours_before']}", flush=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "table_booking_reminder_scheduler"


def _persist() -> None:
    if _sb is None:
        return
    app_settings.set_setting(_sb, _SETTINGS_KEY, {
        "enabled": _state["enabled"],
        "interval_minutes": _state["interval_minutes"],
        "hours_before": _state["hours_before"],
    })


def _load_persisted() -> None:
    if _sb is None:
        return
    try:
        saved = app_settings.get_setting(_sb, _SETTINGS_KEY, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[table_booking_reminder_scheduler] failed to load persisted settings: {exc}", flush=True)
        return
    if not saved:
        return
    if "enabled" in saved:
        _state["enabled"] = bool(saved["enabled"])
    if "interval_minutes" in saved:
        _state["interval_minutes"] = int(saved["interval_minutes"])
    if "hours_before" in saved:
        _state["hours_before"] = int(saved["hours_before"])


# ---------------------------------------------------------------------------
# SMS credentials (same app_settings key as table_booking_routes.py)
# ---------------------------------------------------------------------------

def _maybe_decrypt(enc: Optional[str]) -> Optional[str]:
    if not enc:
        return None
    try:
        return decrypt_value(enc)
    except ValueError:
        return None


def _sms_credentials(sb: Client) -> Optional[dict]:
    saved = app_settings.get_setting(sb, _SMS_SETTINGS_KEY, {}) or {}
    api_key = _maybe_decrypt(saved.get("api_key_enc"))
    access_key = _maybe_decrypt(saved.get("access_key_enc"))
    client_id = saved.get("client_id")
    if not (api_key and access_key and client_id):
        return None
    return {
        "api_key": api_key,
        "access_key": access_key,
        "client_id": client_id,
        "sender_id": saved.get("sender_id") or "OnfonInfo",
    }


# ---------------------------------------------------------------------------
# Reminder content
# ---------------------------------------------------------------------------

def _reminder_action_buttons_html(
    directions_url: Optional[str], calendar_url: Optional[str], menu_url: Optional[str] = None,
) -> str:
    buttons = []
    if directions_url:
        buttons.append(
            f'<a href="{directions_url}" style="display:inline-block;background:#087f3b;color:#fff;'
            f'padding:10px 16px;border-radius:8px;margin:0 8px 8px 0;text-decoration:none;font-size:13px;font-weight:600;">'
            f'Get Directions</a>'
        )
    if calendar_url:
        buttons.append(
            f'<a href="{calendar_url}" style="display:inline-block;background:#fff;color:#1a1a1a;'
            f'border:1px solid #d1d5db;padding:9px 16px;border-radius:8px;text-decoration:none;'
            f'font-size:13px;font-weight:600;margin:0 8px 8px 0;">'
            f'Add to Google Calendar</a>'
        )
    if menu_url:
        buttons.append(
            f'<a href="{menu_url}" style="display:inline-block;background:#fff;color:#1a1a1a;'
            f'border:1px solid #d1d5db;padding:9px 16px;border-radius:8px;text-decoration:none;'
            f'font-size:13px;font-weight:600;margin:0 8px 8px 0;">'
            f'View Menu</a>'
        )
    if not buttons:
        return ""
    return f'<div style="margin-top:4px;">{"".join(buttons)}</div>'


def _reminder_email_html(
    booking: dict, location_name: str, directions_url: Optional[str],
    calendar_url: Optional[str] = None, menu_url: Optional[str] = None,
) -> tuple[str, str]:
    subject = f"Artcaffe — See you soon at {location_name}"
    html = notification_service.render_email(f"""
    <p style="font-size:16px;font-weight:700;color:#1a1a1a;">See you soon!</p>
    <p style="font-size:14px;color:#374151;">Hi {booking['customer_name']}, this is a reminder about your upcoming table booking.</p>
    <div style="background:#f6f8f6;border:1px solid #e2e7e3;border-radius:10px;padding:14px;margin:14px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Location:</strong> {location_name}</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']}</p>
      <p style="margin:0 0 6px;"><strong>Time:</strong> {booking['booking_time']}</p>
      <p style="margin:0;"><strong>Party size:</strong> {booking['party_size']}</p>
    </div>
    {_reminder_action_buttons_html(directions_url, calendar_url, menu_url)}

""", heading=f"Artcaffe", category="Table booking", preheader=subject)
    return subject, html


def _reminder_sms_text(booking: dict, location_name: str, directions_url: Optional[str]) -> str:
    suffix = f" Directions: {directions_url}" if directions_url else ""
    return (
        f"Artcaffe: Reminder — your table for {booking['party_size']} at {location_name} "
        f"today at {booking['booking_time']} is coming up. See you soon!{suffix}"
    )


def _branch_reminder_email_html(booking: dict, location_name: str) -> tuple[str, str]:
    """Same reminder sent to the customer, but addressed to the branch —
    a heads-up that this booking is coming up soon, same style as the
    branch's original new-booking email in table_booking_routes.py."""
    subject = f"{location_name} — Upcoming booking reminder: {booking['customer_name']} ({booking['party_size']} pax)"
    html = notification_service.render_email(f"""
    <p style="font-size:15px;color:#374151;">Reminder — this booking is coming up soon.</p>
    <div style="background:#f6f8f6;border:1px solid #e2e7e3;border-radius:10px;padding:14px;margin:14px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Guest:</strong> {booking['customer_name']} ({booking['phone']}, {booking['email']})</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']} at {booking['booking_time']}</p>
      <p style="margin:0 0 6px;"><strong>Party size:</strong> {booking['party_size']}</p>
      <p style="margin:0;"><strong>Seating:</strong> {booking['seating_preference'].title()}</p>
    </div>
    <a href="{notification_service.DASHBOARD_URL}/table-bookings"
       style="display:inline-block;background:#087f3b;color:#fff;padding:10px 16px;
              border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
      View in Table Bookings
    </a>

""", heading=f"Artcaffe — {location_name}", category="Table booking", preheader=subject)
    return subject, html


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _booking_datetime_eat(booking: dict) -> Optional[datetime]:
    try:
        y, m, d = (int(x) for x in booking["booking_date"].split("-")[:3])
        hh, mm = (int(x) for x in booking["booking_time"].split(":")[:2])
        return datetime(y, m, d, hh, mm, tzinfo=EAT)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _get_candidate_bookings(sb: Client) -> list[dict]:
    """Confirmed bookings not yet reminder-attempted, whose date is
    within a day either side of 'today' EAT — a cheap DB-level
    prefilter; the precise hours-before window check happens in Python
    against real UTC time in _run_once_sync."""
    today_eat = _now_utc().astimezone(EAT).date()
    date_from = (today_eat - timedelta(days=1)).isoformat()
    date_to = (today_eat + timedelta(days=2)).isoformat()
    res = (
        sb.table("table_bookings")
        .select("id,location_id,customer_name,party_size,booking_date,booking_time,phone,email,seating_preference,status")
        .eq("status", "confirmed")
        .is_("reminder_sent_at", "null")
        .gte("booking_date", date_from)
        .lte("booking_date", date_to)
        .execute()
    )
    return res.data or []


def _reminder_calendar_url(booking: dict, location: dict) -> Optional[str]:
    return ics_helper.google_calendar_url(
        summary=f"Table booking at {location.get('name') or 'Artcaffe'}",
        booking_date=booking["booking_date"],
        booking_time=booking["booking_time"],
        location_name=location.get("name") or "Artcaffe",
        location_address=location.get("address"),
        description=f"Party of {booking['party_size']} — {booking['seating_preference'].title()} seating.",
    )


def _reminder_ics_attachment(booking: dict, location: dict) -> Optional[list]:
    ics = ics_helper.build_ics(
        summary=f"Table booking at {location.get('name') or 'Artcaffe'}",
        booking_date=booking["booking_date"],
        booking_time=booking["booking_time"],
        location_name=location.get("name") or "Artcaffe",
        location_address=location.get("address"),
        description=f"Party of {booking['party_size']} — {booking['seating_preference'].title()} seating.",
    )
    if not ics:
        return None
    return [{
        "filename": "table-booking.ics",
        "content": list(ics.encode("utf-8")),
        "content_type": "text/calendar",
    }]


def _send_reminder(sb: Client, booking: dict, location: dict) -> bool:
    """Fires the reminder email and SMS independently — one channel
    failing never blocks the other — then marks reminder_sent_at
    regardless of outcome so this booking is never retried. Returns
    True if at least one channel actually delivered."""
    location_name = location.get("name") or "Artcaffe"
    directions_url = _directions_url(location) if location else None
    calendar_url = _reminder_calendar_url(booking, location)
    menu_url = (location or {}).get("menu_url")
    ics_attachment = _reminder_ics_attachment(booking, location)
    update: dict = {}
    any_sent = False

    subject, html = _reminder_email_html(booking, location_name, directions_url, calendar_url, menu_url)
    try:
        sent = notification_service._send_email(booking["email"], subject, html, attachments=ics_attachment)
        update["reminder_email_sent"] = sent
        update["reminder_email_error"] = None if sent else "Email provider not configured or send failed"
        any_sent = any_sent or sent
    except Exception as exc:  # noqa: BLE001
        update["reminder_email_sent"] = False
        update["reminder_email_error"] = str(exc)[:300]

    creds = _sms_credentials(sb)
    if creds:
        try:
            onfon_sms_connector.send_sms(
                to_number=booking["phone"],
                text=_reminder_sms_text(booking, location_name, directions_url),
                api_key=creds["api_key"],
                client_id=creds["client_id"],
                access_key=creds["access_key"],
                sender_id=creds["sender_id"],
            )
            update["reminder_sms_sent"] = True
            update["reminder_sms_error"] = None
            any_sent = True
        except Exception as exc:  # noqa: BLE001
            update["reminder_sms_sent"] = False
            update["reminder_sms_error"] = str(exc)[:300]
    else:
        update["reminder_sms_sent"] = False
        update["reminder_sms_error"] = "SMS credentials not configured"

    branch_email = (location or {}).get("branch_email")
    if branch_email:
        try:
            b_subject, b_html = _branch_reminder_email_html(booking, location_name)
            b_sent = notification_service._send_email(branch_email, b_subject, b_html)
            update["reminder_branch_email_sent"] = b_sent
            update["reminder_branch_email_error"] = None if b_sent else "Email provider not configured or send failed"
            any_sent = any_sent or b_sent
        except Exception as exc:  # noqa: BLE001
            update["reminder_branch_email_sent"] = False
            update["reminder_branch_email_error"] = str(exc)[:300]
    else:
        update["reminder_branch_email_sent"] = False
        update["reminder_branch_email_error"] = "No branch email configured for this location"

    update["reminder_sent_at"] = _now_utc().isoformat()
    try:
        sb.table("table_bookings").update(update).eq("id", booking["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        # Persisting the attempt failed — logged, not raised, so one bad
        # row can't take down the whole run for every other booking due.
        print(
            f"[table_booking_reminder_scheduler] failed to persist reminder status "
            f"for booking {booking.get('id')}: {exc}",
            flush=True,
        )
    return any_sent


def send_reminder_now(sb: Client, booking_id: str) -> bool:
    """Manual, admin-triggered resend (table_booking_routes.py's
    POST /{booking_id}/resend-reminder) — bypasses the scheduled loop's
    due-window and already-attempted checks entirely, since this is an
    explicit one-off retry, not a re-arming of the automatic reminder.
    Reuses _send_reminder so both paths share the exact same per-channel
    error handling."""
    res = (
        sb.table("table_bookings")
        .select("id,location_id,customer_name,party_size,booking_date,booking_time,phone,email,seating_preference,status")
        .eq("id", booking_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        print(f"[table_booking_reminder_scheduler] send_reminder_now: booking {booking_id} not found", flush=True)
        return False
    booking = res.data

    location: dict = {}
    if booking.get("location_id"):
        loc_res = (
            sb.table("locations")
            .select("id,name,address,latitude,longitude,google_place_id,branch_email,menu_url")
            .eq("id", booking["location_id"])
            .maybe_single()
            .execute()
        )
        location = loc_res.data or {} if loc_res else {}

    return _send_reminder(sb, booking, location)


def _run_once_sync(sb: Client) -> dict:
    candidates = _get_candidate_bookings(sb)
    if not candidates:
        return {"checked": 0, "sent": 0}

    hours_before = _state["hours_before"]
    now = _now_utc()
    due = []
    for b in candidates:
        dt = _booking_datetime_eat(b)
        if not dt:
            continue
        hours_until = (dt - now).total_seconds() / 3600
        if 0 <= hours_until <= hours_before:
            due.append(b)

    if not due:
        return {"checked": len(candidates), "sent": 0}

    loc_ids = list({b["location_id"] for b in due if b.get("location_id")})
    locations_by_id: dict[str, dict] = {}
    if loc_ids:
        loc_res = (
            sb.table("locations")
            .select("id,name,address,latitude,longitude,google_place_id,branch_email,menu_url")
            .in_("id", loc_ids)
            .execute()
        )
        locations_by_id = {loc["id"]: loc for loc in (loc_res.data or [])}

    sent = 0
    for b in due:
        loc = locations_by_id.get(b.get("location_id")) or {}
        if _send_reminder(sb, b, loc):
            sent += 1

    return {"checked": len(candidates), "sent": sent}


async def _scheduler_loop() -> None:
    print(
        f"[table_booking_reminder_scheduler] loop started — "
        f"interval={_state['interval_minutes']}min  hours_before={_state['hours_before']}  enabled={_state['enabled']}",
        flush=True,
    )
    while True:
        interval = _state["interval_minutes"]
        _state["next_run_at"] = (_now_utc() + timedelta(minutes=interval)).isoformat()
        await asyncio.sleep(interval * 60)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_persisted)

        if not _state["enabled"]:
            continue

        from scheduler_lock import try_claim_run  # noqa: PLC0415
        claimed = await loop.run_in_executor(
            None, try_claim_run, _sb, "table_booking_reminder_scheduler", interval * 60 - 5,
        )
        if not claimed:
            continue

        _state["run_count"] += 1
        run_num = _state["run_count"]
        _state["last_run_at"] = _now_utc().isoformat()
        _state["next_run_at"] = None
        _state["last_error"] = None

        try:
            result = await loop.run_in_executor(None, _run_once_sync, _sb)
            _state["last_reminders_sent"] = result.get("sent", 0)
            if result.get("sent"):
                print(
                    f"[table_booking_reminder_scheduler] run #{run_num} — "
                    f"checked={result.get('checked')} sent={result.get('sent')}",
                    flush=True,
                )
        except Exception as exc:
            err_str = f"{exc.__class__.__name__}: {exc}"
            _state["last_error"] = err_str[:300]
            print(
                f"[table_booking_reminder_scheduler] run #{run_num} failed: {err_str}\n{traceback.format_exc()}",
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
        print("[table_booking_reminder_scheduler] already running", flush=True)
        return
    _task = asyncio.create_task(_scheduler_loop())
    print(f"[table_booking_reminder_scheduler] task created — first run in {_state['interval_minutes']} min", flush=True)


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    print("[table_booking_reminder_scheduler] stopped", flush=True)

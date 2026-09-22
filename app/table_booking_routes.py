"""
table_booking_routes.py
--------------------------
Table booking submissions for the dine-in reservation form. The form
itself lives in the frontend (frontend/src/routes/book-table.tsx) as a
standalone, unauthenticated page the user embeds into the Shopify
storefront via <iframe> themselves — this backend never touches the
Shopify theme.

Two routers, mirroring locations_routes.py + locations_public_routes.py's
exact admin/public split:

  Public (no auth — CORS allowlist + nothing secret returned):
    POST /api/public/table-bookings

  Admin (X-Api-Key, module-local require_api_key copy):
    GET   /table-bookings
    PATCH /table-bookings/{id}
    GET   /table-bookings/settings
    POST  /table-bookings/settings
    POST  /table-bookings/settings/test-sms
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

import app_settings
import ics_helper
import notification_service
from connectors import onfon_sms_connector
from locations_public_routes import _directions_url
from secrets_crypto import decrypt_value, encrypt_value, mask_value

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

SETTINGS_KEY = "sms_provider_credentials"
LARGE_PARTY_THRESHOLD = 12


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


public_router = APIRouter(prefix="/api/public")
router = APIRouter(prefix="/table-bookings", dependencies=[Depends(require_api_key)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SMS credentials (app_settings, same pattern as tiktok_routes.py)
# ---------------------------------------------------------------------------
def _sms_settings() -> dict:
    return app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}


def _maybe_decrypt(enc: Optional[str]) -> Optional[str]:
    if not enc:
        return None
    try:
        return decrypt_value(enc)
    except ValueError:
        return None


def _sms_credentials() -> Optional[dict]:
    """Returns {api_key, client_id, access_key, sender_id} or None if not
    fully configured."""
    saved = _sms_settings()
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
# Public: create booking
# ---------------------------------------------------------------------------
class BookingCreate(BaseModel):
    location_id: str
    customer_name: str
    party_size: int
    booking_date: str
    booking_time: str
    phone: str
    email: str
    special_occasion: Optional[str] = None
    seating_preference: str


def _calendar_url(booking: dict, location: dict) -> Optional[str]:
    return ics_helper.google_calendar_url(
        summary=f"Table booking at {location.get('name') or 'Artcaffe'}",
        booking_date=booking["booking_date"],
        booking_time=booking["booking_time"],
        location_name=location.get("name") or "Artcaffe",
        location_address=location.get("address"),
        description=f"Party of {booking['party_size']} — {booking['seating_preference'].title()} seating.",
    )


def _ics_attachment(booking: dict, location: dict) -> Optional[list]:
    """Resend attachment list for the .ics calendar invite, or None if
    the booking's date/time couldn't be parsed into an event."""
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


def _action_buttons_html(
    directions_url: Optional[str], calendar_url: Optional[str], menu_url: Optional[str] = None,
) -> str:
    buttons = []
    if directions_url:
        buttons.append(
            f'<a href="{directions_url}" style="display:inline-block;background:#1a1a1a;color:#fff;'
            f'padding:10px 20px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">'
            f'Get Directions</a>'
        )
    if calendar_url:
        buttons.append(
            f'<a href="{calendar_url}" style="display:inline-block;background:#fff;color:#1a1a1a;'
            f'border:1px solid #d1d5db;padding:9px 20px;border-radius:6px;text-decoration:none;'
            f'font-size:13px;font-weight:600;margin-left:8px;">'
            f'Add to Google Calendar</a>'
        )
    if menu_url:
        buttons.append(
            f'<a href="{menu_url}" style="display:inline-block;background:#fff;color:#1a1a1a;'
            f'border:1px solid #d1d5db;padding:9px 20px;border-radius:6px;text-decoration:none;'
            f'font-size:13px;font-weight:600;margin-left:8px;">'
            f'View Menu</a>'
        )
    if not buttons:
        return ""
    return f'<div style="margin-top:4px;">{"".join(buttons)}</div>'


def _customer_email_html(
    booking: dict, location_name: str, confirmed: bool,
    directions_url: Optional[str] = None, calendar_url: Optional[str] = None, menu_url: Optional[str] = None,
) -> tuple[str, str]:
    if confirmed:
        subject = f"Artcaffe — Your table at {location_name} is confirmed"
        headline = "Your table is confirmed!"
        body = (
            f"We look forward to seeing you on <strong>{booking['booking_date']} at "
            f"{booking['booking_time']}</strong> for a party of {booking['party_size']}."
        )
    else:
        subject = f"Artcaffe — We've received your booking request for {location_name}"
        headline = "We'll contact you within the hour"
        body = (
            f"Thanks for your booking request for a party of {booking['party_size']} on "
            f"<strong>{booking['booking_date']} at {booking['booking_time']}</strong>. "
            f"Groups larger than {LARGE_PARTY_THRESHOLD} need a quick check with the team — "
            f"we'll call or message you shortly to confirm."
        )
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:16px;font-weight:700;color:#1a1a1a;">{headline}</p>
    <p style="font-size:14px;color:#374151;">Hi {booking['customer_name']},</p>
    <p style="font-size:14px;color:#374151;">{body}</p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Location:</strong> {location_name}</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']}</p>
      <p style="margin:0 0 6px;"><strong>Time:</strong> {booking['booking_time']}</p>
      <p style="margin:0 0 6px;"><strong>Party size:</strong> {booking['party_size']}</p>
      <p style="margin:0;"><strong>Seating:</strong> {booking['seating_preference'].title()}</p>
    </div>
    {_action_buttons_html(directions_url, calendar_url, menu_url)}
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe</p>
  </div>
</div>
"""
    return subject, html


def _customer_sms_text(booking: dict, location_name: str, confirmed: bool, directions_url: Optional[str] = None) -> str:
    directions_suffix = f" Directions: {directions_url}" if directions_url else ""
    if confirmed:
        return (
            f"Artcaffe: Your table at {location_name} for {booking['party_size']} on "
            f"{booking['booking_date']} at {booking['booking_time']} is confirmed. See you then!"
            f"{directions_suffix}"
        )
    return (
        f"Artcaffe: We've received your booking request at {location_name} for "
        f"{booking['party_size']} on {booking['booking_date']} at {booking['booking_time']}. "
        f"We'll contact you within the hour to confirm.{directions_suffix}"
    )


def _staff_sms_text(booking: dict, location_name: str) -> str:
    return (
        f"New table booking ({booking['status']}): {booking['customer_name']}, "
        f"party of {booking['party_size']}, {location_name}, {booking['booking_date']} "
        f"{booking['booking_time']}. Phone: {booking['phone']}."
    )


def _cancellation_email_html(booking: dict, location_name: str) -> tuple[str, str]:
    subject = f"Artcaffe — Your booking at {location_name} has been cancelled"
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:16px;font-weight:700;color:#1a1a1a;">Your booking has been cancelled</p>
    <p style="font-size:14px;color:#374151;">Hi {booking['customer_name']}, your table booking below has been cancelled.</p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Location:</strong> {location_name}</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']}</p>
      <p style="margin:0;"><strong>Time:</strong> {booking['booking_time']}</p>
    </div>
    <p style="font-size:14px;color:#374151;">If this wasn't expected, or you'd like to book again, just get in touch or submit a new request.</p>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe</p>
  </div>
</div>
"""
    return subject, html


def _cancellation_sms_text(booking: dict, location_name: str) -> str:
    return (
        f"Artcaffe: Your booking at {location_name} for {booking['booking_date']} at "
        f"{booking['booking_time']} has been cancelled. Contact us if you have questions."
    )


def _send_cancellation_notification(booking: dict, location: dict) -> None:
    """Customer-facing cancellation email + SMS — fired only when an admin
    explicitly opts in via the cancel confirmation dialog, each channel
    independently try/excepted like every other notification here."""
    location_name = (location or {}).get("name") or "Artcaffe"
    update: dict = {}

    subject, html = _cancellation_email_html(booking, location_name)
    try:
        sent = notification_service._send_email(booking["email"], subject, html)
        update["cancellation_email_sent"] = sent
        update["cancellation_email_error"] = None if sent else "Email provider not configured or send failed"
    except Exception as exc:  # noqa: BLE001
        update["cancellation_email_sent"] = False
        update["cancellation_email_error"] = str(exc)[:300]

    creds = _sms_credentials()
    if creds:
        try:
            onfon_sms_connector.send_sms(
                to_number=booking["phone"],
                text=_cancellation_sms_text(booking, location_name),
                api_key=creds["api_key"],
                client_id=creds["client_id"],
                access_key=creds["access_key"],
                sender_id=creds["sender_id"],
            )
            update["cancellation_sms_sent"] = True
            update["cancellation_sms_error"] = None
        except Exception as exc:  # noqa: BLE001
            update["cancellation_sms_sent"] = False
            update["cancellation_sms_error"] = str(exc)[:300]
    else:
        update["cancellation_sms_sent"] = False
        update["cancellation_sms_error"] = "SMS credentials not configured"

    update["updated_at"] = _now()
    sb.table("table_bookings").update(update).eq("id", booking["id"]).execute()


def _admin_notification_html(booking: dict, location_name: str) -> tuple[str, str]:
    needs_action = booking["status"] == "pending"
    subject = (
        f"Artcaffe — Booking needs confirmation: {booking['customer_name']} ({booking['party_size']} pax)"
        if needs_action else
        f"Artcaffe — New table booking: {booking['customer_name']} ({booking['party_size']} pax)"
    )
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe AI Marketing</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">
      {"A party of over " + str(LARGE_PARTY_THRESHOLD) + " needs your confirmation." if needs_action else "A new table booking was confirmed automatically."}
    </p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Guest:</strong> {booking['customer_name']} ({booking['phone']})</p>
      <p style="margin:0 0 6px;"><strong>Location:</strong> {location_name}</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']} at {booking['booking_time']}</p>
      <p style="margin:0 0 6px;"><strong>Party size:</strong> {booking['party_size']}</p>
      <p style="margin:0;"><strong>Seating:</strong> {booking['seating_preference'].title()}</p>
    </div>
    <a href="{notification_service.DASHBOARD_URL}/table-bookings"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      {"Review in Table Bookings" if needs_action else "View in Table Bookings"}
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
    return subject, html


def _branch_email_html(booking: dict, location_name: str) -> tuple[str, str]:
    needs_action = booking["status"] == "pending"
    subject = f"{location_name} — New booking: {booking['customer_name']} ({booking['party_size']} pax)"
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe — {location_name}</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">
      {"A party of over " + str(LARGE_PARTY_THRESHOLD) + " has requested a table and needs confirmation." if needs_action else "A new table booking has come in for your branch."}
    </p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;font-size:13px;color:#374151;">
      <p style="margin:0 0 6px;"><strong>Guest:</strong> {booking['customer_name']} ({booking['phone']}, {booking['email']})</p>
      <p style="margin:0 0 6px;"><strong>Date:</strong> {booking['booking_date']} at {booking['booking_time']}</p>
      <p style="margin:0 0 6px;"><strong>Party size:</strong> {booking['party_size']}</p>
      <p style="margin:0;"><strong>Seating:</strong> {booking['seating_preference'].title()}</p>
    </div>
    <a href="{notification_service.DASHBOARD_URL}/table-bookings"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      View in Table Bookings
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
    return subject, html


def _resend_branch_email_only(booking: dict, location: dict) -> None:
    """Standalone resend of just the branch email — a separate DB write
    from _send_booking_notifications since it touches only one channel,
    used by POST /{booking_id}/resend-branch-email."""
    location_name = location.get("name") or "Artcaffe"
    branch_email = location.get("branch_email")
    update: dict = {}
    if branch_email:
        try:
            subject, html = _branch_email_html(booking, location_name)
            sent = notification_service._send_email(branch_email, subject, html)
            update["branch_email_sent"] = sent
            update["branch_email_error"] = None if sent else "Email provider not configured or send failed"
        except Exception as exc:  # noqa: BLE001
            update["branch_email_sent"] = False
            update["branch_email_error"] = str(exc)[:300]
    else:
        update["branch_email_sent"] = False
        update["branch_email_error"] = "No branch email configured for this location"
    update["updated_at"] = _now()
    sb.table("table_bookings").update(update).eq("id", booking["id"]).execute()


def _notify_admins_of_booking(booking: dict, location_name: str) -> None:
    """Fans out to every active admin/content_manager who hasn't opted out
    (team inbox row + audit log + email), same convention as approval_needed
    and post_scheduled/published. Best-effort — never raises."""
    try:
        subject, html = _admin_notification_html(booking, location_name)
        notification_service._notify_relevant_team(
            sb,
            notif_type="table_booking_created",
            subject=subject,
            html=html,
            payload={"booking_id": booking["id"], "location_id": booking["location_id"], "status": booking["status"]},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[table_booking_routes] admin notify failed: {exc}", flush=True)


def _send_booking_notifications(
    booking: dict, location: dict, notify_admins: bool = True,
) -> None:
    """Fires customer email, customer SMS, staff SMS, branch-manager email,
    and (on first creation only) an admin team notification — each
    independently, so one channel failing never blocks another, and the
    row always reflects exactly what did/didn't go out."""
    location = location or {}
    location_name = location.get("name") or "Artcaffe"
    directions_url = _directions_url(location) if location else None
    calendar_url = _calendar_url(booking, location)
    menu_url = location.get("menu_url")
    ics_attachment = _ics_attachment(booking, location)

    if notify_admins:
        _notify_admins_of_booking(booking, location_name)

    confirmed = booking["status"] == "confirmed"
    update: dict = {}

    subject, html = _customer_email_html(booking, location_name, confirmed, directions_url, calendar_url, menu_url)
    try:
        sent = notification_service._send_email(booking["email"], subject, html, attachments=ics_attachment)
        update["email_sent"] = sent
        update["email_error"] = None if sent else "Email provider not configured or send failed"
    except Exception as exc:  # noqa: BLE001
        update["email_sent"] = False
        update["email_error"] = str(exc)[:300]

    branch_email = location.get("branch_email")
    if branch_email:
        try:
            b_subject, b_html = _branch_email_html(booking, location_name)
            b_sent = notification_service._send_email(branch_email, b_subject, b_html)
            update["branch_email_sent"] = b_sent
            update["branch_email_error"] = None if b_sent else "Email provider not configured or send failed"
        except Exception as exc:  # noqa: BLE001
            update["branch_email_sent"] = False
            update["branch_email_error"] = str(exc)[:300]
    else:
        update["branch_email_sent"] = False
        update["branch_email_error"] = "No branch email configured for this location"

    creds = _sms_credentials()
    if creds:
        try:
            onfon_sms_connector.send_sms(
                to_number=booking["phone"],
                text=_customer_sms_text(booking, location_name, confirmed, directions_url),
                api_key=creds["api_key"],
                client_id=creds["client_id"],
                access_key=creds["access_key"],
                sender_id=creds["sender_id"],
            )
            update["sms_sent"] = True
            update["sms_error"] = None
        except Exception as exc:  # noqa: BLE001
            update["sms_sent"] = False
            update["sms_error"] = str(exc)[:300]

        staff_phones = _sms_settings().get("staff_notification_phones") or []
        if staff_phones:
            failures = []
            successes = 0
            for phone in staff_phones:
                try:
                    onfon_sms_connector.send_sms(
                        to_number=phone,
                        text=_staff_sms_text(booking, location_name),
                        api_key=creds["api_key"],
                        client_id=creds["client_id"],
                        access_key=creds["access_key"],
                        sender_id=creds["sender_id"],
                    )
                    successes += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{phone}: {str(exc)[:100]}")
            # At least one number reached counts as notified — a booking
            # shouldn't read as "staff not notified" when only one of
            # several numbers failed.
            update["staff_notified"] = successes > 0
            update["staff_notify_error"] = "; ".join(failures)[:300] if failures else None
        else:
            update["staff_notify_error"] = "No staff notification phone configured"
    else:
        update["sms_sent"] = False
        update["sms_error"] = "SMS credentials not configured"
        update["staff_notify_error"] = "SMS credentials not configured"

    update["updated_at"] = _now()
    sb.table("table_bookings").update(update).eq("id", booking["id"]).execute()


@public_router.post("/table-bookings")
def create_booking(body: BookingCreate, bg: BackgroundTasks):
    if body.party_size <= 0:
        raise HTTPException(400, "Party size must be at least 1")
    if body.seating_preference not in ("flexible", "inside", "outside"):
        raise HTTPException(400, "Invalid seating preference")

    loc_res = (
        sb.table("locations")
        .select("id,name,address,latitude,longitude,google_place_id,branch_email,menu_url")
        .eq("id", body.location_id)
        .eq("status", "active")
        .maybe_single()
        .execute()
    )
    if not loc_res or not loc_res.data:
        raise HTTPException(400, "Unknown or inactive location")
    location = loc_res.data

    status = "confirmed" if body.party_size <= LARGE_PARTY_THRESHOLD else "pending"
    row = {
        **body.dict(),
        "status": status,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        res = sb.table("table_bookings").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not create booking: {exc}") from exc
    booking = (res.data or [None])[0]
    if not booking:
        raise HTTPException(500, "Booking was not created")

    bg.add_task(_send_booking_notifications, booking, location)
    return {
        "ok": True,
        "booking_id": booking["id"],
        "status": status,
        "calendar_url": _calendar_url(booking, location),
    }


# ---------------------------------------------------------------------------
# Admin: list, single booking, edit + operations
# ---------------------------------------------------------------------------
def _enrich_bookings(bookings: list[dict]) -> None:
    """Joins each row's location_name/location_brand/location_directions_url
    in place — shared by list_bookings, get_booking, and update_booking's
    response so every admin-facing booking shape carries the same fields."""
    loc_ids = list({b["location_id"] for b in bookings if b.get("location_id")})
    locations_by_id: dict[str, dict] = {}
    if loc_ids:
        loc_res = (
            sb.table("locations")
            .select("id,name,brand_type,address,latitude,longitude,google_place_id")
            .in_("id", loc_ids)
            .execute()
        )
        locations_by_id = {loc["id"]: loc for loc in (loc_res.data or [])}

    for b in bookings:
        loc = locations_by_id.get(b.get("location_id")) or {}
        b["location_name"] = loc.get("name")
        b["location_brand"] = loc.get("brand_type")
        b["location_directions_url"] = _directions_url(loc) if loc else None


@router.get("")
def list_bookings(
    location_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    q = sb.table("table_bookings").select("*").order("booking_date", desc=True).order("created_at", desc=True)
    if location_id:
        q = q.eq("location_id", location_id)
    if status:
        q = q.eq("status", status)
    if date_from:
        q = q.gte("booking_date", date_from)
    if date_to:
        q = q.lte("booking_date", date_to)
    res = q.execute()
    bookings = res.data or []
    _enrich_bookings(bookings)
    return {"ok": True, "bookings": bookings}


class BookingUpdate(BaseModel):
    status: Optional[str] = None
    party_size: Optional[int] = None
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None
    seating_preference: Optional[str] = None
    special_occasion: Optional[str] = None
    # Not a table_bookings column — popped out of the DB update below.
    # Set when the admin's cancel confirmation dialog opts in to also
    # notifying the customer.
    notify_customer: Optional[bool] = None


@router.patch("/{booking_id}")
def update_booking(booking_id: str, body: BookingUpdate, bg: BackgroundTasks):
    if body.status is not None and body.status not in ("pending", "confirmed", "declined", "cancelled"):
        raise HTTPException(400, "Invalid status")
    if body.seating_preference is not None and body.seating_preference not in ("flexible", "inside", "outside"):
        raise HTTPException(400, "Invalid seating preference")
    if body.party_size is not None and body.party_size <= 0:
        raise HTTPException(400, "Party size must be at least 1")

    res = sb.table("table_bookings").select("*").eq("id", booking_id).maybe_single().execute()
    if not res or not res.data:
        raise HTTPException(404, "Booking not found")
    existing = res.data

    update = body.dict(exclude_unset=True)
    notify_customer_on_cancel = update.pop("notify_customer", None)
    if not update:
        raise HTTPException(400, "No fields to update")
    update["updated_at"] = _now()

    try:
        upd_res = sb.table("table_bookings").update(update).eq("id", booking_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not update booking: {exc}") from exc
    booking = (upd_res.data or [None])[0]
    if not booking:
        raise HTTPException(404, "Booking not found")
    _enrich_bookings([booking])

    # Newly confirmed from pending, and notifications hadn't already gone out
    # — fire them now the same way the auto-confirm path does.
    if body.status == "confirmed" and existing["status"] != "confirmed" and not existing.get("sms_sent") and not existing.get("email_sent"):
        loc_res = (
            sb.table("locations")
            .select("name,address,latitude,longitude,google_place_id,branch_email,menu_url")
            .eq("id", booking["location_id"])
            .maybe_single()
            .execute()
        )
        location = loc_res.data or {} if loc_res else {}
        # notify_admins=False — the admin acting here already knows.
        bg.add_task(_send_booking_notifications, booking, location, notify_admins=False)

    # Newly cancelled, and the admin opted in via the cancel confirmation
    # dialog to also notifying the customer.
    if body.status == "cancelled" and existing["status"] != "cancelled" and notify_customer_on_cancel:
        loc_res = (
            sb.table("locations")
            .select("name")
            .eq("id", booking["location_id"])
            .maybe_single()
            .execute()
        )
        location = loc_res.data or {} if loc_res else {}
        bg.add_task(_send_cancellation_notification, booking, location)

    return {"ok": True, "booking": booking}


@router.post("/{booking_id}/resend-confirmation")
def resend_confirmation(booking_id: str, bg: BackgroundTasks):
    """Manually re-fires the customer confirmation email/SMS + staff SMS +
    branch email — regardless of prior sent state, since this is an
    explicit admin retry after noticing a failed channel."""
    res = sb.table("table_bookings").select("*").eq("id", booking_id).maybe_single().execute()
    if not res or not res.data:
        raise HTTPException(404, "Booking not found")
    booking = res.data

    loc_res = (
        sb.table("locations")
        .select("name,address,latitude,longitude,google_place_id,branch_email,menu_url")
        .eq("id", booking["location_id"])
        .maybe_single()
        .execute()
    )
    location = loc_res.data or {} if loc_res else {}
    bg.add_task(_send_booking_notifications, booking, location, notify_admins=False)
    return {"ok": True, "message": "Resending confirmation email/SMS"}


@router.post("/{booking_id}/resend-branch-email")
def resend_branch_email(booking_id: str, bg: BackgroundTasks):
    """Manually re-fires just the branch/manager email — independent of
    the customer confirmation resend, since a branch notification
    failure shouldn't require re-sending the customer's own email too."""
    res = sb.table("table_bookings").select("*").eq("id", booking_id).maybe_single().execute()
    if not res or not res.data:
        raise HTTPException(404, "Booking not found")
    booking = res.data

    loc_res = (
        sb.table("locations")
        .select("name,branch_email")
        .eq("id", booking["location_id"])
        .maybe_single()
        .execute()
    )
    location = loc_res.data or {} if loc_res else {}
    if not location.get("branch_email"):
        raise HTTPException(400, "No branch email configured for this location")

    bg.add_task(_resend_branch_email_only, booking, location)
    return {"ok": True, "message": f"Resending branch email to {location['branch_email']}"}


@router.post("/{booking_id}/resend-reminder")
def resend_reminder(booking_id: str, bg: BackgroundTasks):
    """Manually re-fires the reminder email/SMS, bypassing the scheduler's
    own due-window and already-attempted checks — an explicit one-off
    retry, not a re-arming of the scheduled reminder."""
    from table_booking_reminder_scheduler import send_reminder_now  # noqa: PLC0415

    res = sb.table("table_bookings").select("id").eq("id", booking_id).maybe_single().execute()
    if not res or not res.data:
        raise HTTPException(404, "Booking not found")
    bg.add_task(send_reminder_now, sb, booking_id)
    return {"ok": True, "message": "Resending reminder email/SMS"}



# ---------------------------------------------------------------------------
# Admin: SMS settings
# ---------------------------------------------------------------------------
class SmsSettingsUpdate(BaseModel):
    api_key: Optional[str] = None
    access_key: Optional[str] = None
    client_id: Optional[str] = None
    sender_id: Optional[str] = None
    staff_notification_phones: Optional[List[str]] = None


@router.get("/settings")
def get_sms_settings():
    saved = _sms_settings()
    return {
        "ok": True,
        "data": {
            "client_id": saved.get("client_id"),
            "sender_id": saved.get("sender_id") or "OnfonInfo",
            "staff_notification_phones": saved.get("staff_notification_phones") or [],
            "api_key_masked": mask_value(_maybe_decrypt(saved.get("api_key_enc"))),
            "api_key_configured": bool(saved.get("api_key_enc")),
            "access_key_masked": mask_value(_maybe_decrypt(saved.get("access_key_enc"))),
            "access_key_configured": bool(saved.get("access_key_enc")),
            "last_test_status": saved.get("last_test_status"),
            "last_test_at": saved.get("last_test_at"),
            "last_test_error": saved.get("last_test_error"),
        },
    }


@router.post("/settings")
def update_sms_settings(body: SmsSettingsUpdate):
    saved = _sms_settings()
    if body.api_key:
        saved["api_key_enc"] = encrypt_value(body.api_key)
    if body.access_key:
        saved["access_key_enc"] = encrypt_value(body.access_key)
    if body.client_id is not None:
        saved["client_id"] = body.client_id
    if body.sender_id is not None:
        saved["sender_id"] = body.sender_id
    if body.staff_notification_phones is not None:
        saved["staff_notification_phones"] = [p.strip() for p in body.staff_notification_phones if p.strip()]
    app_settings.set_setting(sb, SETTINGS_KEY, saved)
    return get_sms_settings()


@router.post("/settings/test-sms")
def send_test_sms():
    """No safe read-only endpoint exists on Onfon's side — this sends a
    real SMS to every configured staff phone, labeled "Send test SMS" in
    the UI rather than "Test connection" to set the right expectation."""
    saved = _sms_settings()
    staff_phones = saved.get("staff_notification_phones") or []
    now = _now()
    if not staff_phones:
        raise HTTPException(400, "Set at least one staff notification phone number first")
    creds = _sms_credentials()
    if not creds:
        raise HTTPException(400, "SMS credentials not fully configured")

    failures = []
    for phone in staff_phones:
        try:
            onfon_sms_connector.send_sms(
                to_number=phone,
                text="Artcaffe: this is a test SMS from Table Booking settings.",
                api_key=creds["api_key"],
                client_id=creds["client_id"],
                access_key=creds["access_key"],
                sender_id=creds["sender_id"],
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{phone}: {str(exc)[:150]}")

    if failures:
        error = "; ".join(failures)[:300]
        saved["last_test_status"] = "failed"
        saved["last_test_at"] = now
        saved["last_test_error"] = error
        app_settings.set_setting(sb, SETTINGS_KEY, saved)
        return {"ok": False, "error": error}

    saved["last_test_status"] = "success"
    saved["last_test_at"] = now
    saved["last_test_error"] = None
    app_settings.set_setting(sb, SETTINGS_KEY, saved)
    return {"ok": True, "message": f"Test SMS sent to {', '.join(staff_phones)}"}


# ---------------------------------------------------------------------------
# Admin: reminder scheduler settings
# ---------------------------------------------------------------------------
class ReminderSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = None
    hours_before: Optional[int] = None


@router.get("/reminders/settings")
def get_reminder_settings():
    from table_booking_reminder_scheduler import get_state  # noqa: PLC0415
    return {"ok": True, "data": get_state()}


@router.post("/reminders/settings")
def update_reminder_settings(body: ReminderSettingsUpdate):
    from table_booking_reminder_scheduler import (  # noqa: PLC0415
        set_enabled, set_interval, set_hours_before, get_state,
    )
    if body.interval_minutes is not None:
        set_interval(body.interval_minutes)
    if body.hours_before is not None:
        set_hours_before(body.hours_before)
    if body.enabled is not None:
        set_enabled(body.enabled)
    return {"ok": True, "data": get_state()}


# Registered last of all GET routes on this router — a literal
# single-segment path like /settings would otherwise be swallowed by
# this catch-all {booking_id}, matching the ordering rule documented
# in locations_routes.py.
@router.get("/{booking_id}")
def get_booking(booking_id: str):
    res = sb.table("table_bookings").select("*").eq("id", booking_id).maybe_single().execute()
    if not res or not res.data:
        raise HTTPException(404, "Booking not found")
    booking = res.data
    _enrich_bookings([booking])
    return {"ok": True, "booking": booking}

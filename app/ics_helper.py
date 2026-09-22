"""
ics_helper.py
---------------
Minimal iCalendar (.ics) VEVENT generator and Google Calendar "quick add"
link builder — lets a customer add their table booking to their own
calendar. Neither needs external API credentials: an .ics file is a
plain text format every major calendar app (Google, Outlook, Apple)
can import from an email attachment, and Google's quick-add link is a
documented URL format, not an authenticated API call — so there's no
OAuth relationship with the customer to set up for either.

booking_date/booking_time are treated as Africa/Nairobi wall-clock
time, matching every other place in this codebase that handles a
booking's date/time (table_booking_routes.py, table_booking_reminder_
scheduler.py) via the same fixed UTC+3 offset (Kenya has no DST).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

EAT = timezone(timedelta(hours=3))
DEFAULT_DURATION_HOURS = 2


def _booking_datetime_eat(booking_date: str, booking_time: str) -> Optional[datetime]:
    try:
        y, m, d = (int(x) for x in booking_date.split("-")[:3])
        hh, mm = (int(x) for x in booking_time.split(":")[:2])
        return datetime(y, m, d, hh, mm, tzinfo=EAT)
    except (ValueError, TypeError, AttributeError):
        return None


def _escape_ics_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_ics(
    *,
    summary: str,
    booking_date: str,
    booking_time: str,
    location_name: str,
    location_address: Optional[str] = None,
    description: Optional[str] = None,
    duration_hours: int = DEFAULT_DURATION_HOURS,
) -> Optional[str]:
    """Returns a standalone .ics file's text content (single VEVENT), or
    None if booking_date/booking_time couldn't be parsed."""
    start = _booking_datetime_eat(booking_date, booking_time)
    if not start:
        return None
    end = start + timedelta(hours=duration_hours)

    def fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    loc = f"{location_name}, {location_address}" if location_address else location_name
    uid = f"{uuid.uuid4()}@artcaffe.co.ke"
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Artcaffe//Table Booking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_stamp}",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SUMMARY:{_escape_ics_text(summary)}",
        f"LOCATION:{_escape_ics_text(loc)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape_ics_text(description)}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    # iCalendar requires CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


def google_calendar_url(
    *,
    summary: str,
    booking_date: str,
    booking_time: str,
    location_name: str,
    location_address: Optional[str] = None,
    description: Optional[str] = None,
    duration_hours: int = DEFAULT_DURATION_HOURS,
) -> Optional[str]:
    """Google Calendar's documented 'quick add' render URL — opens a
    pre-filled event creation screen, no API key or auth needed."""
    start = _booking_datetime_eat(booking_date, booking_time)
    if not start:
        return None
    end = start + timedelta(hours=duration_hours)

    def fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    loc = f"{location_name}, {location_address}" if location_address else location_name
    params = {
        "action": "TEMPLATE",
        "text": summary,
        "dates": f"{fmt(start)}/{fmt(end)}",
        "location": loc,
    }
    if description:
        params["details"] = description
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"https://calendar.google.com/calendar/render?{query}"

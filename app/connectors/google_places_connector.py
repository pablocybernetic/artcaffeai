"""
google_places_connector.py
----------------------------
Pulls place data from the Google Places API (New) — https://places.googleapis.com/v1/
— not the deprecated legacy Places API the Shopify theme currently calls
client-side. Mirrors meta_organic_connector.py's plain-httpx style.

Only ever called server-side with the Places server API key (from
Admin Settings, decrypted in-process) — this key must never reach a
public response or a browser.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

PLACES_BASE = "https://places.googleapis.com/v1"

# Every field the sync service might need, in one field mask — cheaper
# than issuing a second request, and the API only bills for fields
# actually present in the mask regardless of whether we use all of them.
_FIELD_MASK = ",".join([
    "id",
    "displayName",
    "formattedAddress",
    "addressComponents",
    "location",
    "internationalPhoneNumber",
    "nationalPhoneNumber",
    "rating",
    "userRatingCount",
    "regularOpeningHours",
    "googleMapsUri",
    "businessStatus",
    "photos",
    "reviews",
    "primaryType",
])

# A safe, stable place_id to validate an API key against without depending
# on any customer data existing yet — Google's own Sydney office. Used by
# the "Test Connection" button.
TEST_PLACE_ID = "ChIJP3Sa8ziYEmsRUKgyFmh9AQM"


def _get(url: str, *, api_key: str, params: Optional[dict] = None, timeout: float = 15.0) -> dict:
    resp = httpx.get(
        url,
        params=params,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": _FIELD_MASK,
        },
        timeout=timeout,
    )
    if not resp.is_success:
        raise RuntimeError(f"Google Places API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def fetch_place_details(place_id: str, api_key: str) -> dict:
    """Raises RuntimeError on any non-2xx response (bad key, invalid
    place_id, rate limit, etc) — callers decide how to log/surface it."""
    return _get(f"{PLACES_BASE}/places/{place_id}", api_key=api_key)


def resolve_photo_uris(photo_names: list[str], api_key: str, limit: int = 5) -> list[dict]:
    """Resolves each Google photo reference to a keyless googleusercontent
    CDN URL via the Photo Media endpoint, at sync time — never construct a
    photo URL containing the API key, since that would leak the server
    key to anyone who sees a public API response."""
    resolved: list[dict] = []
    for name in photo_names[:limit]:
        try:
            resp = httpx.get(
                f"{PLACES_BASE}/{name}/media",
                params={"maxWidthPx": 1200, "skipHttpRedirect": "true"},
                headers={"X-Goog-Api-Key": api_key},
                timeout=15.0,
            )
            if resp.is_success:
                data = resp.json()
                if data.get("photoUri"):
                    resolved.append({"url": data["photoUri"], "name": name})
        except Exception:  # noqa: BLE001
            continue  # one bad photo shouldn't fail the whole sync
    return resolved


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _normalize_opening_hours(regular_hours: Optional[dict]) -> dict:
    """Maps New Places API's periods (Google's week starts Sunday=0) into
    {"monday": {"open": "06:00", "close": "00:00", "closed": false}, ...}
    per spec §18 — cached so Shopify never needs a live Google call just
    to answer "are you open now"."""
    hours: dict[str, dict] = {
        day: {"open": None, "close": None, "closed": True} for day in _WEEKDAY_NAMES
    }
    if not regular_hours:
        return hours

    for period in regular_hours.get("periods", []):
        open_pt = period.get("open") or {}
        close_pt = period.get("close") or {}
        google_day = open_pt.get("day")  # 0=Sunday .. 6=Saturday
        if google_day is None:
            continue
        day_name = _WEEKDAY_NAMES[(google_day - 1) % 7]
        hours[day_name] = {
            "open": f"{open_pt.get('hour', 0):02d}:{open_pt.get('minute', 0):02d}",
            "close": f"{close_pt.get('hour', 0):02d}:{close_pt.get('minute', 0):02d}",
            "closed": False,
        }
    return hours


def _normalize_reviews(raw_reviews: list[dict]) -> list[dict]:
    """Google's Places API (New) caps this at up to 5 "most relevant"
    reviews — never assume this is the complete review history."""
    reviews = []
    for r in raw_reviews or []:
        author = r.get("authorAttribution") or {}
        reviews.append({
            "author_name": author.get("displayName"),
            "rating": r.get("rating"),
            "text": (r.get("text") or {}).get("text"),
            "relative_time_description": r.get("relativePublishTimeDescription"),
            "published_at": r.get("publishTime"),
            "profile_photo_url": author.get("photoUri"),
            "google_review_reference": r.get("name"),
        })
    return reviews


def normalize_place_details(raw: dict, api_key: str) -> dict:
    """Maps a Google Places API (New) place resource into our locations
    column shape. Only ever touches Google-managed fields — callers must
    not blindly write the whole dict over a location row (see
    google_location_sync_service.py's per-field sync toggle handling)."""
    location = raw.get("location") or {}
    photos_raw = raw.get("photos") or []

    return {
        "name": (raw.get("displayName") or {}).get("text"),
        "formatted_address": raw.get("formattedAddress"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "phone": raw.get("nationalPhoneNumber"),
        "international_phone": raw.get("internationalPhoneNumber"),
        "rating": raw.get("rating"),
        "review_count": raw.get("userRatingCount", 0),
        "opening_hours": _normalize_opening_hours(raw.get("regularOpeningHours")),
        "google_maps_url": raw.get("googleMapsUri"),
        "business_status": raw.get("businessStatus"),  # OPERATIONAL | CLOSED_TEMPORARILY | CLOSED_PERMANENTLY
        "google_reviews": _normalize_reviews(raw.get("reviews") or []),
        "google_photos": resolve_photo_uris([p["name"] for p in photos_raw if p.get("name")], api_key),
        "google_raw_data": raw,
    }


def normalize_place_details_offline(raw: dict) -> dict:
    """Same as normalize_place_details but skips photo resolution (no
    extra API calls) — used for quick tests/dry runs that don't need
    images."""
    location = raw.get("location") or {}
    return {
        "name": (raw.get("displayName") or {}).get("text"),
        "formatted_address": raw.get("formattedAddress"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "phone": raw.get("nationalPhoneNumber"),
        "international_phone": raw.get("internationalPhoneNumber"),
        "rating": raw.get("rating"),
        "review_count": raw.get("userRatingCount", 0),
        "opening_hours": _normalize_opening_hours(raw.get("regularOpeningHours")),
        "google_maps_url": raw.get("googleMapsUri"),
        "business_status": raw.get("businessStatus"),
        "google_reviews": _normalize_reviews(raw.get("reviews") or []),
        "google_photos": [],
        "google_raw_data": raw,
    }

"""
google_location_sync_service.py
---------------------------------
GoogleLocationSyncService: reads active locations with a google_place_id,
pulls fresh data from Google Places, writes only the Google-managed
columns (preserving everything MarketingAI manages), and logs every
attempt to location_sync_logs.

Field-level control: DEFAULT_SYNC_FIELDS lists every toggleable field from
the Admin Settings checkbox list (spec §1); a location only gets a field
written if that field's toggle is on in settings AND the location itself
has sync_from_google=true.

`status` is handled conservatively: Google's businessStatus only ever
pushes our status to temporarily_closed/permanently_closed, and only
pulls it back to active if this same sync service is what closed it in
the first place — an admin-set inactive/coming_soon is never silently
overwritten (see _apply_business_status).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client

from connectors.google_places_connector import fetch_place_details, normalize_place_details

DEFAULT_SYNC_FIELDS = {
    "business_name": True,
    "address": True,
    "coordinates": True,
    "phone": True,
    "opening_hours": True,
    "rating": True,
    "review_count": True,
    "google_maps_url": True,
    "business_status": True,
    "reviews": True,
    "photos": True,
}

_GOOGLE_CLOSED_STATUSES = {"temporarily_closed", "permanently_closed"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _apply_business_status(current_status: str, business_status: Optional[str], was_google_closed: bool) -> Optional[str]:
    """Returns a new status value, or None if status shouldn't change.
    Never overwrites an admin-set inactive/coming_soon."""
    if business_status == "CLOSED_TEMPORARILY":
        return "temporarily_closed"
    if business_status == "CLOSED_PERMANENTLY":
        return "permanently_closed"
    if business_status == "OPERATIONAL" and current_status in _GOOGLE_CLOSED_STATUSES and was_google_closed:
        return "active"
    return None


def sync_location(sb: Client, location: dict, api_key: str, sync_fields: Optional[dict] = None) -> dict:
    """Syncs one location row. Returns {status, fields_changed, error}."""
    fields = {**DEFAULT_SYNC_FIELDS, **(sync_fields or {})}
    location_id = location["id"]
    place_id = location.get("google_place_id")

    log_row = {
        "location_id": location_id,
        "provider": "google",
        "status": "syncing",
        "started_at": _now_iso(),
    }

    if not place_id:
        return _finish(sb, location_id, log_row, status="failed", error="No google_place_id set", response_code=None)

    try:
        raw = fetch_place_details(place_id, api_key)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:500]
        _write_sync_failure(sb, location_id, err)
        return _finish(sb, location_id, log_row, status="failed", error=err, response_code=None)

    try:
        normalized = normalize_place_details(raw, api_key)
    except Exception as exc:  # noqa: BLE001
        err = f"Failed to normalize Google response: {exc}"[:500]
        _write_sync_failure(sb, location_id, err)
        return _finish(sb, location_id, log_row, status="failed", error=err, response_code=None)

    update: dict[str, Any] = {}
    fields_changed: list[str] = []

    def _maybe_set(toggle: str, column: str, value: Any) -> None:
        if not fields.get(toggle, True):
            return
        if location.get(column) != value:
            fields_changed.append(column)
        update[column] = value

    if fields.get("business_name", True) and normalized.get("name"):
        _maybe_set("business_name", "name", normalized["name"])
    if fields.get("address", True) and normalized.get("formatted_address"):
        _maybe_set("address", "formatted_address", normalized["formatted_address"])
    if fields.get("coordinates", True):
        if normalized.get("latitude") is not None:
            _maybe_set("coordinates", "latitude", normalized["latitude"])
        if normalized.get("longitude") is not None:
            _maybe_set("coordinates", "longitude", normalized["longitude"])
    if fields.get("phone", True):
        if normalized.get("phone"):
            _maybe_set("phone", "phone", normalized["phone"])
        if normalized.get("international_phone"):
            _maybe_set("phone", "international_phone", normalized["international_phone"])
    if fields.get("opening_hours", True):
        _maybe_set("opening_hours", "opening_hours", normalized["opening_hours"])
    if fields.get("rating", True) and normalized.get("rating") is not None:
        _maybe_set("rating", "rating", normalized["rating"])
    if fields.get("review_count", True):
        _maybe_set("review_count", "review_count", normalized.get("review_count", 0))
    if fields.get("google_maps_url", True) and normalized.get("google_maps_url"):
        _maybe_set("google_maps_url", "google_maps_url", normalized["google_maps_url"])
    if fields.get("reviews", True):
        _maybe_set("reviews", "google_reviews", normalized["google_reviews"])
    if fields.get("photos", True):
        _maybe_set("photos", "google_photos", normalized["google_photos"])

    if fields.get("business_status", True):
        was_google_closed = location.get("last_google_sync_status") == "completed" and location.get("status") in _GOOGLE_CLOSED_STATUSES
        new_status = _apply_business_status(location.get("status", "active"), normalized.get("business_status"), was_google_closed)
        if new_status and new_status != location.get("status"):
            update["status"] = new_status
            fields_changed.append("status")

    update["google_raw_data"] = normalized.get("google_raw_data")
    update["last_google_sync_at"] = _now_iso()
    update["last_google_sync_status"] = "completed"
    update["last_google_sync_error"] = None
    update["updated_at"] = _now_iso()

    sb.table("locations").update(update).eq("id", location_id).execute()

    return _finish(sb, location_id, log_row, status="completed", error=None, response_code=200, fields_changed=fields_changed)


def _write_sync_failure(sb: Client, location_id: str, error: str) -> None:
    # Preserve every existing column — only touch the sync-status metadata,
    # per spec §39 ("a temporary Google outage must never remove data").
    sb.table("locations").update({
        "last_google_sync_at": _now_iso(),
        "last_google_sync_status": "failed",
        "last_google_sync_error": error,
        "updated_at": _now_iso(),
    }).eq("id", location_id).execute()


def _finish(
    sb: Client,
    location_id: str,
    log_row: dict,
    *,
    status: str,
    error: Optional[str],
    response_code: Optional[int],
    fields_changed: Optional[list[str]] = None,
) -> dict:
    log_row.update({
        "status": status,
        "completed_at": _now_iso(),
        "fields_changed": fields_changed or [],
        "error_message": error,
        "response_code": response_code,
    })
    try:
        sb.table("location_sync_logs").insert(log_row).execute()
    except Exception:  # noqa: BLE001
        pass  # logging failure shouldn't mask the sync result itself
    return {"location_id": location_id, "status": status, "error": error, "fields_changed": fields_changed or []}


def sync_all_locations(sb: Client, api_key: str, sync_fields: Optional[dict] = None, batch_size: int = 3, pause_seconds: float = 1.0) -> dict:
    """Syncs every active, sync_from_google-enabled location with a
    google_place_id, in small batches with a pause between them — per
    spec §11, never fire dozens of Google requests simultaneously."""
    res = (
        sb.table("locations")
        .select("*")
        .eq("status", "active")
        .eq("sync_from_google", True)
        .not_.is_("google_place_id", "null")
        .execute()
    )
    locations = res.data or []

    updated = 0
    no_changes = 0
    failed = 0
    results: list[dict] = []

    for i in range(0, len(locations), batch_size):
        batch = locations[i:i + batch_size]
        for loc in batch:
            result = sync_location(sb, loc, api_key, sync_fields)
            results.append(result)
            if result["status"] == "failed":
                failed += 1
            elif result["fields_changed"]:
                updated += 1
            else:
                no_changes += 1
        if i + batch_size < len(locations):
            time.sleep(pause_seconds)

    return {"updated": updated, "no_changes": no_changes, "failed": failed, "results": results}

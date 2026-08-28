"""
locations_routes.py
---------------------
Admin-facing Locations CRUD, Google integration settings, sync
triggers/history, and internal health/cron endpoints.

Auth: X-Api-Key header (require_api_key, module-local copy — every
route file in this app defines its own, not a shared import) for
everything under /locations and /api/internal/locations. The
/api/internal/jobs/google-location-sync cron-trigger endpoint uses a
separate CRON_SECRET bearer-token dependency instead, since it's meant
to be hit by a scheduler, not a logged-in admin session.

Public, unauthenticated Locations reads live in locations_public_routes.py.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

import app_settings
import locations_scheduler
from connectors.google_places_connector import fetch_place_details, TEST_PLACE_ID
from google_location_sync_service import sync_location, sync_all_locations
from secrets_crypto import encrypt_value, mask_value

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
CRON_SECRET = os.environ.get("CRON_SECRET")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

SETTINGS_KEY = locations_scheduler.SETTINGS_KEY


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def require_cron_secret(authorization: Optional[str] = Header(None)) -> None:
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET not configured")
    expected = f"Bearer {CRON_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid cron secret")


router = APIRouter(prefix="/locations", dependencies=[Depends(require_api_key)])
internal_router = APIRouter(prefix="/api/internal")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class LocationIn(BaseModel):
    brand_type: str
    name: str
    slug: Optional[str] = None
    store_code: Optional[str] = None
    google_place_id: Optional[str] = None
    google_business_location_id: Optional[str] = None
    status: Optional[str] = "active"
    country: Optional[str] = "Kenya"
    county: Optional[str] = None
    city: Optional[str] = None
    area: Optional[str] = None
    address: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    phone: Optional[str] = None
    website_url: Optional[str] = None
    shopify_url: Optional[str] = None
    menu_url: Optional[str] = None
    reservation_url: Optional[str] = None
    order_url: Optional[str] = None
    short_description: Optional[str] = None
    description: Optional[str] = None
    primary_category: Optional[str] = None
    secondary_categories: Optional[list] = None
    services: Optional[list] = None
    amenities: Optional[list] = None
    nearby_landmarks: Optional[list] = None
    opening_hours: Optional[dict] = None
    hero_image_url: Optional[str] = None
    gallery: Optional[list] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    schema_type: Optional[str] = None
    display_order: Optional[int] = None
    sync_from_google: Optional[bool] = True
    google_business_profile_url: Optional[str] = None
    tripadvisor_url: Optional[str] = None
    facebook_url: Optional[str] = None
    instagram_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    uber_eats_url: Optional[str] = None
    glovo_url: Optional[str] = None
    media_mentions: Optional[list] = None


class LocationUpdate(BaseModel):
    """Same fields as LocationIn, all optional — PATCH semantics via
    exclude_unset so omitted fields are left untouched."""
    brand_type: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    store_code: Optional[str] = None
    google_place_id: Optional[str] = None
    google_business_location_id: Optional[str] = None
    status: Optional[str] = None
    country: Optional[str] = None
    county: Optional[str] = None
    city: Optional[str] = None
    area: Optional[str] = None
    address: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    phone: Optional[str] = None
    website_url: Optional[str] = None
    shopify_url: Optional[str] = None
    menu_url: Optional[str] = None
    reservation_url: Optional[str] = None
    order_url: Optional[str] = None
    short_description: Optional[str] = None
    description: Optional[str] = None
    primary_category: Optional[str] = None
    secondary_categories: Optional[list] = None
    services: Optional[list] = None
    amenities: Optional[list] = None
    nearby_landmarks: Optional[list] = None
    opening_hours: Optional[dict] = None
    hero_image_url: Optional[str] = None
    gallery: Optional[list] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    schema_type: Optional[str] = None
    display_order: Optional[int] = None
    sync_from_google: Optional[bool] = None
    google_business_profile_url: Optional[str] = None
    tripadvisor_url: Optional[str] = None
    facebook_url: Optional[str] = None
    instagram_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    uber_eats_url: Optional[str] = None
    glovo_url: Optional[str] = None
    media_mentions: Optional[list] = None


@router.get("")
def list_locations(
    brand: Optional[str] = None,
    city: Optional[str] = None,
    county: Optional[str] = None,
    status: Optional[str] = None,
    google_sync_status: Optional[str] = None,
    search: Optional[str] = None,
):
    q = sb.table("locations").select("*").order("display_order").order("name")
    if brand:
        q = q.eq("brand_type", brand)
    if city:
        q = q.eq("city", city)
    if county:
        q = q.eq("county", county)
    if status:
        q = q.eq("status", status)
    if google_sync_status:
        q = q.eq("last_google_sync_status", google_sync_status)
    res = q.execute()
    rows = res.data or []
    if search:
        term = search.lower()
        rows = [
            r for r in rows
            if term in (r.get("name") or "").lower()
            or term in (r.get("address") or "").lower()
            or term in (r.get("city") or "").lower()
            or term in (r.get("area") or "").lower()
            or term in (r.get("store_code") or "").lower()
            or term in (r.get("google_place_id") or "").lower()
        ]
    return {"ok": True, "locations": rows}


@router.get("/{location_id}")
def get_location(location_id: str):
    res = sb.table("locations").select("*").eq("id", location_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(404, "Location not found")
    return {"ok": True, "location": res.data}


@router.post("")
def create_location(body: LocationIn):
    row = body.dict()
    if not row.get("slug"):
        row["slug"] = _slugify(row["name"])
    row["created_at"] = _now()
    row["updated_at"] = _now()
    try:
        res = sb.table("locations").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not create location: {exc}") from exc
    return {"ok": True, "location": (res.data or [None])[0]}


@router.patch("/{location_id}")
def update_location(location_id: str, body: LocationUpdate):
    update = body.dict(exclude_unset=True)
    if not update:
        raise HTTPException(400, "No fields to update")
    update["updated_at"] = _now()
    try:
        res = sb.table("locations").update(update).eq("id", location_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not update location: {exc}") from exc
    if not res.data:
        raise HTTPException(404, "Location not found")
    return {"ok": True, "location": res.data[0]}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _places_api_key() -> str:
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    enc = saved.get("places_api_key_enc")
    if not enc:
        raise HTTPException(400, "Google Places API key not configured — set it under Settings → Integrations")
    from secrets_crypto import decrypt_value  # noqa: PLC0415
    try:
        return decrypt_value(enc)
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/{location_id}/sync")
def sync_one_location(location_id: str):
    res = sb.table("locations").select("*").eq("id", location_id).maybe_single().execute()
    if not res.data:
        raise HTTPException(404, "Location not found")
    api_key = _places_api_key()
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    result = sync_location(sb, res.data, api_key, sync_fields=saved.get("sync_fields"))
    return {"ok": result["status"] != "failed", "result": result}


@router.post("/sync-all")
def sync_all():
    api_key = _places_api_key()
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    summary = sync_all_locations(sb, api_key, sync_fields=saved.get("sync_fields"))
    return {"ok": True, **summary}


@router.get("/meta/sync-logs")
def list_sync_logs(
    location_id: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
):
    q = sb.table("location_sync_logs").select("*").order("started_at", desc=True).limit(min(limit, 500))
    if location_id:
        q = q.eq("location_id", location_id)
    if status:
        q = q.eq("status", status)
    if since:
        q = q.gte("started_at", since)
    res = q.execute()
    return {"ok": True, "logs": res.data or []}


# ---------------------------------------------------------------------------
# Settings (Google credentials + sync configuration)
# ---------------------------------------------------------------------------
_CREDENTIAL_FIELDS = ["places_api_key", "maps_browser_api_key"]
_PLAIN_FIELDS = ["gbp_account_id", "gbp_location_group_id", "gcp_project_id"]


class LocationsSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    sync_frequency: Optional[str] = None
    places_api_key: Optional[str] = None
    maps_browser_api_key: Optional[str] = None
    gbp_account_id: Optional[str] = None
    gbp_location_group_id: Optional[str] = None
    gcp_project_id: Optional[str] = None
    sync_fields: Optional[dict] = None


@router.get("/settings")
def get_settings():
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    scheduler_state = locations_scheduler.get_state()
    return {
        "ok": True,
        "data": {
            "enabled": scheduler_state["enabled"],
            "sync_frequency": scheduler_state["sync_frequency"],
            "sync_fields": saved.get("sync_fields") or {},
            "gbp_account_id": saved.get("gbp_account_id"),
            "gbp_location_group_id": saved.get("gbp_location_group_id"),
            "gcp_project_id": saved.get("gcp_project_id"),
            "places_api_key_masked": mask_value(_maybe_decrypt(saved.get("places_api_key_enc"))),
            "maps_browser_api_key_masked": mask_value(_maybe_decrypt(saved.get("maps_browser_api_key_enc"))),
            "places_api_key_configured": bool(saved.get("places_api_key_enc")),
            "maps_browser_api_key_configured": bool(saved.get("maps_browser_api_key_enc")),
            "last_test_status": saved.get("last_test_status"),
            "last_test_at": saved.get("last_test_at"),
            "last_test_error": saved.get("last_test_error"),
            "last_success_at": saved.get("last_success_at"),
            "last_failure_at": saved.get("last_failure_at"),
            "last_run_at": scheduler_state["last_run_at"],
            "next_run_at": scheduler_state["next_run_at"],
            "run_count": scheduler_state["run_count"],
            "last_result": scheduler_state["last_result"],
            "last_error": scheduler_state["last_error"],
        },
    }


def _maybe_decrypt(enc: Optional[str]) -> Optional[str]:
    if not enc:
        return None
    from secrets_crypto import decrypt_value  # noqa: PLC0415
    try:
        return decrypt_value(enc)
    except ValueError:
        return None


@router.post("/settings")
def update_settings(body: LocationsSettingsUpdate):
    if body.sync_frequency is not None:
        try:
            locations_scheduler.set_sync_frequency(body.sync_frequency)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.enabled is not None:
        locations_scheduler.set_enabled(body.enabled)

    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    if body.places_api_key:
        saved["places_api_key_enc"] = encrypt_value(body.places_api_key)
    if body.maps_browser_api_key:
        saved["maps_browser_api_key_enc"] = encrypt_value(body.maps_browser_api_key)
    for field in _PLAIN_FIELDS:
        value = getattr(body, field)
        if value is not None:
            saved[field] = value
    if body.sync_fields is not None:
        saved["sync_fields"] = {**(saved.get("sync_fields") or {}), **body.sync_fields}
    app_settings.set_setting(sb, SETTINGS_KEY, saved)

    return get_settings()


@router.post("/settings/remove-credential")
def remove_credential(field: str):
    if field not in _CREDENTIAL_FIELDS:
        raise HTTPException(400, f"Unknown credential field: {field}")
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    saved.pop(f"{field}_enc", None)
    app_settings.set_setting(sb, SETTINGS_KEY, saved)
    return get_settings()


@router.post("/settings/test-connection")
def test_connection():
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    api_key = _maybe_decrypt(saved.get("places_api_key_enc"))
    now = _now()
    if not api_key:
        saved["last_test_status"] = "failed"
        saved["last_test_at"] = now
        saved["last_test_error"] = "No Google Places API key configured"
        app_settings.set_setting(sb, SETTINGS_KEY, saved)
        return {"ok": False, "error": "No Google Places API key configured"}

    try:
        fetch_place_details(TEST_PLACE_ID, api_key)
        saved["last_test_status"] = "success"
        saved["last_test_at"] = now
        saved["last_test_error"] = None
        saved["last_success_at"] = now
        app_settings.set_setting(sb, SETTINGS_KEY, saved)
        return {"ok": True, "message": "Connected successfully"}
    except Exception as exc:  # noqa: BLE001
        # Sanitized — never echo the raw exception (which could embed the key
        # in a request-URL-based error message) back to the client.
        error = "Google API request failed — check the API key is valid and Places API (New) is enabled for it."
        saved["last_test_status"] = "failed"
        saved["last_test_at"] = now
        saved["last_test_error"] = error
        saved["last_failure_at"] = now
        app_settings.set_setting(sb, SETTINGS_KEY, saved)
        return {"ok": False, "error": error}


# ---------------------------------------------------------------------------
# Internal: health + cron trigger
# ---------------------------------------------------------------------------
@internal_router.get("/locations/health", dependencies=[Depends(require_api_key)])
def locations_health():
    res = sb.table("locations").select("id,status,google_place_id,last_google_sync_status,last_google_sync_at").execute()
    rows = res.data or []
    active = [r for r in rows if r["status"] == "active"]
    missing_place_id = [r for r in active if not r.get("google_place_id")]
    failed = [r for r in active if r.get("last_google_sync_status") == "failed"]

    stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stale = []
    for r in active:
        if not r.get("google_place_id"):
            continue
        ts = r.get("last_google_sync_at")
        if not ts or datetime.fromisoformat(ts.replace("Z", "+00:00")) < stale_cutoff:
            stale.append(r)

    scheduler_state = locations_scheduler.get_state()
    last_result = scheduler_state.get("last_result") or {}
    return {
        "ok": True,
        "active_locations": len(active),
        "locations_missing_place_id": len(missing_place_id),
        "failed_locations": len(failed),
        "stale_locations": len(stale),
        "last_sync": scheduler_state.get("last_run_at"),
        "last_successful_sync": scheduler_state.get("last_run_at") if not scheduler_state.get("last_error") else None,
        "last_run_summary": last_result,
    }


@internal_router.post("/jobs/google-location-sync", dependencies=[Depends(require_cron_secret)])
def trigger_google_location_sync():
    """Secondary manual-trigger path alongside the in-process scheduler —
    e.g. for a real Linux cron hitting this endpoint instead of relying
    solely on locations_scheduler.py's own timer."""
    api_key = _places_api_key()
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    summary = sync_all_locations(sb, api_key, sync_fields=saved.get("sync_fields"))
    return {"ok": True, **summary}

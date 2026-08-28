"""
locations_public_routes.py
----------------------------
Public, unauthenticated Locations API — this is what Shopify (both
artcaffe.co.ke and artcaffemarket.co.ke) calls instead of hitting the
Google Places API directly. No X-Api-Key: access is controlled by the
CORS allowlist (see CORS_ORIGINS in app.py) plus the fact that nothing
returned here is secret — never the Places server key, only the
domain-restricted Maps browser key via /api/public/config/maps.

Every response is served from Supabase (synced on a schedule by
locations_scheduler.py) — never a live Google call — and carries a
Cache-Control header so the storefronts and any CDN in front of them
can cache it, per spec §38.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Response
from supabase import Client, create_client

import app_settings
from locations_scheduler import SETTINGS_KEY

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/api/public")

_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=3600"


def _set_cache(response: Response) -> None:
    response.headers["Cache-Control"] = _CACHE_CONTROL


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _serialize_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "brand": row["brand_type"],
        "google_place_id": row.get("google_place_id"),
        "address": {
            "address": row.get("address"),
            "area": row.get("area"),
            "city": row.get("city"),
            "county": row.get("county"),
            "country": row.get("country"),
        },
        "coordinates": {
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
        },
        "phone": row.get("phone"),
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        "opening_hours": row.get("opening_hours") or {},
        "services": row.get("services") or [],
        "google": {
            "maps_url": row.get("google_maps_url"),
            "review_url": row.get("google_review_url"),
        },
        "shopify": {
            "url": row.get("shopify_url"),
        },
        "last_synced_at": row.get("last_google_sync_at"),
    }


def _serialize_detail(row: dict, nearby: Optional[list[dict]] = None) -> dict:
    base = _serialize_summary(row)
    base.update({
        "store_code": row.get("store_code"),
        "status": row.get("status"),
        "international_phone": row.get("international_phone"),
        "website_url": row.get("website_url"),
        "menu_url": row.get("menu_url"),
        "reservation_url": row.get("reservation_url"),
        "order_url": row.get("order_url"),
        "short_description": row.get("short_description"),
        "description": row.get("description"),
        "primary_category": row.get("primary_category"),
        "secondary_categories": row.get("secondary_categories") or [],
        "amenities": row.get("amenities") or [],
        "nearby_landmarks": row.get("nearby_landmarks") or [],
        "hero_image_url": row.get("hero_image_url"),
        "gallery": row.get("gallery") or [],
        "google_reviews": row.get("google_reviews") or [],
        "google_photos": row.get("google_photos") or [],
        "seo": {
            "title": row.get("seo_title"),
            "description": row.get("seo_description"),
        },
        "schema_type": row.get("schema_type"),
        "media": {
            "google_business_profile_url": row.get("google_business_profile_url"),
            "tripadvisor_url": row.get("tripadvisor_url"),
            "facebook_url": row.get("facebook_url"),
            "instagram_url": row.get("instagram_url"),
            "tiktok_url": row.get("tiktok_url"),
            "uber_eats_url": row.get("uber_eats_url"),
            "glovo_url": row.get("glovo_url"),
            "media_mentions": row.get("media_mentions") or [],
        },
    })
    if nearby is not None:
        base["nearby_locations"] = nearby
    return base


@router.get("/locations")
def list_locations(
    response: Response,
    brand: Optional[str] = None,
    city: Optional[str] = None,
    county: Optional[str] = None,
    area: Optional[str] = None,
    service: Optional[str] = None,
    search: Optional[str] = None,
):
    _set_cache(response)
    q = sb.table("locations").select("*").eq("status", "active").order("display_order").order("name")
    if brand:
        q = q.eq("brand_type", brand)
    if city:
        q = q.eq("city", city)
    if county:
        q = q.eq("county", county)
    if area:
        q = q.eq("area", area)
    res = q.execute()
    rows = res.data or []

    if service:
        rows = [r for r in rows if service in (r.get("services") or [])]
    if search:
        term = search.lower()
        rows = [
            r for r in rows
            if term in (r.get("name") or "").lower()
            or term in (r.get("address") or "").lower()
            or term in (r.get("city") or "").lower()
            or term in (r.get("area") or "").lower()
        ]

    return {
        "success": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "locations": [_serialize_summary(r) for r in rows],
    }


@router.get("/locations/nearest")
def nearest_locations(
    response: Response,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    brand: Optional[str] = None,
    service: Optional[str] = None,
    limit: int = 5,
):
    _set_cache(response)
    q = sb.table("locations").select("*").eq("status", "active")
    if brand:
        q = q.eq("brand_type", brand)
    res = q.execute()
    rows = res.data or []
    if service:
        rows = [r for r in rows if service in (r.get("services") or [])]

    results = []
    for r in rows:
        entry = _serialize_summary(r)
        # Never invent a location — distance is only ever computed from
        # coordinates the caller actually supplied (spec §22/§23), not from
        # server/CDN/crawler IP.
        if lat is not None and lng is not None and r.get("latitude") is not None and r.get("longitude") is not None:
            entry["distance_km"] = round(_haversine_km(lat, lng, r["latitude"], r["longitude"]), 2)
        else:
            entry["distance_km"] = None
        results.append(entry)

    if lat is not None and lng is not None:
        results.sort(key=lambda e: (e["distance_km"] is None, e["distance_km"]))

    return {
        "success": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "locations": results[: max(1, min(limit, 50))],
    }


@router.get("/locations/{slug}")
def get_location(slug: str, response: Response):
    _set_cache(response)
    res = sb.table("locations").select("*").eq("slug", slug).eq("status", "active").maybe_single().execute()
    if not res.data:
        raise HTTPException(404, "Location not found")
    row = res.data

    nearby: list[dict] = []
    if row.get("latitude") is not None and row.get("longitude") is not None:
        others_res = (
            sb.table("locations")
            .select("*")
            .eq("status", "active")
            .eq("brand_type", row["brand_type"])
            .neq("id", row["id"])
            .execute()
        )
        for other in others_res.data or []:
            if other.get("latitude") is None or other.get("longitude") is None:
                continue
            dist = _haversine_km(row["latitude"], row["longitude"], other["latitude"], other["longitude"])
            entry = _serialize_summary(other)
            entry["distance_km"] = round(dist, 2)
            nearby.append(entry)
        nearby.sort(key=lambda e: e["distance_km"])
        nearby = nearby[:5]

    return {
        "success": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "location": _serialize_detail(row, nearby=nearby),
    }


@router.get("/locations/{slug}/reviews")
def get_location_reviews(slug: str, response: Response):
    _set_cache(response)
    res = sb.table("locations").select("name,rating,review_count,google_reviews,last_google_sync_at").eq("slug", slug).eq("status", "active").maybe_single().execute()
    if not res.data:
        raise HTTPException(404, "Location not found")
    row = res.data
    return {
        "success": True,
        "name": row["name"],
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        # Google's Places API caps this at ~5 "most relevant" reviews per
        # place — this is not the complete review history.
        "note": "Reviews shown are a sample returned by Google, not the complete review history.",
        "reviews": row.get("google_reviews") or [],
        "last_synced_at": row.get("last_google_sync_at"),
    }


@router.get("/config/maps")
def maps_config(response: Response):
    _set_cache(response)
    saved = app_settings.get_setting(sb, SETTINGS_KEY, {}) or {}
    enc = saved.get("maps_browser_api_key_enc")
    if not enc:
        return {"provider": "google", "browser_api_key": None}
    from secrets_crypto import decrypt_value  # noqa: PLC0415
    try:
        # Only the domain-restricted browser Maps key is ever returned here —
        # never the Places server key, which stays server-side always.
        key = decrypt_value(enc)
    except ValueError:
        key = None
    return {"provider": "google", "browser_api_key": key}

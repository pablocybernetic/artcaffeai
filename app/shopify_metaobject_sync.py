"""
shopify_metaobject_sync.py
-----------------------------
Pushes MarketingAI's artcaffe_market locations into Shopify as
`artcaffe_market_location` metaobject entries, via the Admin GraphQL API.

This is what lets Liquid server-render real branch data (SEO directory,
JSON-LD, initial-HTML store cards) without Shopify ever calling
MarketingAI or Supabase at request time -- Liquid has no server-side
HTTP access, so metaobjects are the only way to get live data into the
page's initial HTML.

Runs after each Google Places sync in locations_scheduler.py, using the
same "shared settings key" pattern as that module -- credentials here
live under SETTINGS_KEY ("locations_shopify_sync"), encrypted at rest
with secrets_crypto (same as the Google Places key), never logged, never
exposed via a public endpoint. The Shopify Admin token stays server-side
always; only Shopify's own read-only, RLS-scoped anon key-equivalent
(the metaobject's public storefront visibility) is what the theme reads
from, and that's controlled entirely by the metaobject definition's own
storefront-access setting in Shopify -- MarketingAI never exposes this
token itself.

Metaobject fields mirror locations_public_routes.py's serializer field
list. Upserts are keyed by handle=slug (unique per location), so
re-running is always idempotent -- no duplicate entries.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

METAOBJECT_TYPE = "artcaffe_market_location"

_UPSERT_MUTATION = """
mutation MetaobjectUpsert($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
  metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
    metaobject { id handle }
    userErrors { field message code }
  }
}
"""


def _gql(store_domain: str, admin_token: str, query: str, variables: dict, timeout: float = 20.0) -> dict:
    resp = httpx.post(
        f"https://{store_domain}/admin/api/2025-01/graphql.json",
        json={"query": query, "variables": variables},
        headers={"X-Shopify-Access-Token": admin_token, "Content-Type": "application/json"},
        timeout=timeout,
    )
    if not resp.is_success:
        raise RuntimeError(f"Shopify Admin API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _build_fields(row: dict) -> list[dict]:
    fields: list[dict] = []

    def add(key: str, value: Any) -> None:
        if value is None or value == "":
            return
        fields.append({"key": key, "value": str(value)})

    add("name", row.get("name"))
    add("slug", row.get("slug"))
    add("store_code", row.get("store_code"))
    add("city", row.get("city"))
    add("county", row.get("county"))
    add("area", row.get("area"))
    add("address", row.get("address") or row.get("formatted_address"))
    add("latitude", row.get("latitude"))
    add("longitude", row.get("longitude"))
    add("phone", row.get("phone"))
    add("google_place_id", row.get("google_place_id"))
    add("google_maps_url", row.get("google_maps_url"))
    add("google_review_url", row.get("google_review_url"))
    services = row.get("services") or []
    if services:
        add("services", ",".join(services))
    add("shopify_url", row.get("shopify_url"))
    opening_hours = row.get("opening_hours")
    if opening_hours:
        add("opening_hours", json.dumps(opening_hours))
    add("rating", row.get("rating"))
    add("review_count", row.get("review_count"))
    add("seo_title", row.get("seo_title"))
    add("seo_description", row.get("seo_description"))
    return fields


def sync_location_to_shopify(store_domain: str, admin_token: str, row: dict) -> dict:
    """Upserts a single location row as a metaobject entry, keyed by
    handle=slug. Raises RuntimeError on a transport-level failure;
    returns {"status": "failed", "error": ...} on a GraphQL-level one so
    one bad row doesn't need special-casing by the caller."""
    handle = row["slug"]
    result = _gql(
        store_domain,
        admin_token,
        _UPSERT_MUTATION,
        {
            "handle": {"type": METAOBJECT_TYPE, "handle": handle},
            "metaobject": {
                "fields": _build_fields(row),
                # The definition has the publishable capability enabled (so
                # Storefront/Liquid access is possible at all) -- entries
                # default to DRAFT and are invisible to shop.metaobjects.*
                # until explicitly published.
                "capabilities": {"publishable": {"status": "ACTIVE"}},
            },
        },
    )
    errors = result.get("errors")
    payload = (result.get("data") or {}).get("metaobjectUpsert") or {}
    user_errors = payload.get("userErrors")
    if errors or user_errors:
        return {"status": "failed", "slug": handle, "error": errors or user_errors}
    return {"status": "ok", "slug": handle, "metaobject_id": (payload.get("metaobject") or {}).get("id")}


def sync_all_to_shopify(sb, store_domain: str, admin_token: str, brand_type: str = "artcaffe_market") -> dict:
    """Syncs every active location of the given brand into Shopify
    metaobjects. Mirrors google_location_sync_service.sync_all_locations's
    return shape (updated/failed/results) for consistency in the admin UI
    and sync-log display."""
    res = (
        sb.table("locations")
        .select("*")
        .eq("brand_type", brand_type)
        .eq("status", "active")
        .execute()
    )
    rows = res.data or []

    results = []
    ok_count = 0
    failed_count = 0
    for row in rows:
        try:
            outcome = sync_location_to_shopify(store_domain, admin_token, row)
        except RuntimeError as exc:
            outcome = {"status": "failed", "slug": row.get("slug"), "error": str(exc)}
        results.append(outcome)
        if outcome["status"] == "ok":
            ok_count += 1
        else:
            failed_count += 1

    return {"synced": ok_count, "failed": failed_count, "total": len(rows), "results": results}

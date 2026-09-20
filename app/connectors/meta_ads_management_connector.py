"""
meta_ads_management_connector.py
---------------------------------
Write path for Meta Marketing API v21.0 — creates/updates campaigns, ad
sets, ad creatives and ads. Mirrors meta_ads_connector.py's raw-httpx
style (no SDK); that connector is read-only reporting, this one mutates.

Credentials required (from platform_credentials table, platform="meta"):
  access_token  — long-lived token with ads_management scope
  ad_account_id — Meta Ad Account ID (with or without "act_" prefix)

Every create_* function hardcodes status="PAUSED" server-side and never
accepts status as a parameter — a deliberate guardrail so a UI bug or
fat-fingered form can never spend real money. update_* functions do
allow status changes, since those are explicit user-initiated edits via
the PATCH endpoints in ads_management_routes.py, not creation defaults.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _account_id(ad_account_id: str) -> str:
    return ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"


def _encode(params: dict) -> dict:
    """Meta's Graph API takes array/object params (targeting, creative,
    special_ad_categories, object_story_spec, ...) as JSON-encoded strings
    when posted as application/x-www-form-urlencoded, not native form
    arrays. None values are dropped entirely so update_* calls built from
    **fields never blank out a field the caller didn't mean to touch."""
    encoded: dict = {}
    for k, v in params.items():
        if v is None:
            continue
        encoded[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    return encoded


def _get(url: str, params: dict, timeout: float = 30.0) -> dict:
    resp = httpx.get(url, params=params, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _post(url: str, data: dict, timeout: float = 30.0) -> dict:
    resp = httpx.post(url, data=data, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
def create_campaign(
    access_token: str,
    ad_account_id: str,
    *,
    name: str,
    objective: str,
    special_ad_categories: Optional[list] = None,
    budget_amount_cents: Optional[int] = None,
    budget_type: Optional[str] = None,
    buying_type: str = "AUCTION",
) -> dict:
    params: dict = {
        "access_token": access_token,
        "name": name,
        "objective": objective,
        # Meta requires this field on every campaign create, even when empty.
        "special_ad_categories": special_ad_categories if special_ad_categories is not None else [],
        "buying_type": buying_type,
        "status": "PAUSED",
    }
    if budget_type == "daily" and budget_amount_cents is not None:
        params["daily_budget"] = budget_amount_cents
    elif budget_type == "lifetime" and budget_amount_cents is not None:
        params["lifetime_budget"] = budget_amount_cents
    return _post(f"{GRAPH_BASE}/{_account_id(ad_account_id)}/campaigns", _encode(params))


def update_campaign(access_token: str, platform_campaign_id: str, **fields) -> dict:
    return _post(f"{GRAPH_BASE}/{platform_campaign_id}", _encode({"access_token": access_token, **fields}))


# ---------------------------------------------------------------------------
# Ad sets
# ---------------------------------------------------------------------------
def create_ad_set(
    access_token: str,
    ad_account_id: str,
    *,
    campaign_id: str,
    name: str,
    targeting: dict,
    optimization_goal: str,
    billing_event: str = "IMPRESSIONS",
    budget_amount_cents: Optional[int] = None,
    budget_type: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    params: dict = {
        "access_token": access_token,
        "campaign_id": campaign_id,
        "name": name,
        "targeting": targeting,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "start_time": start_time,
        "end_time": end_time,
        "status": "PAUSED",
    }
    if budget_type == "daily" and budget_amount_cents is not None:
        params["daily_budget"] = budget_amount_cents
    elif budget_type == "lifetime" and budget_amount_cents is not None:
        params["lifetime_budget"] = budget_amount_cents
    return _post(f"{GRAPH_BASE}/{_account_id(ad_account_id)}/adsets", _encode(params))


def update_ad_set(access_token: str, platform_adset_id: str, **fields) -> dict:
    return _post(f"{GRAPH_BASE}/{platform_adset_id}", _encode({"access_token": access_token, **fields}))


# ---------------------------------------------------------------------------
# Creatives + ads
# ---------------------------------------------------------------------------
def upload_creative_image(access_token: str, ad_account_id: str, image_bytes: bytes, filename: str) -> dict:
    # NOTE: verify this exact shape/mechanism (multipart bytes, response
    # keyed by filename) against live Marketing API docs the first time
    # this is exercised for real — if it errors, the error message will
    # guide the fix, this isn't assumed beyond that.
    resp = httpx.post(
        f"{GRAPH_BASE}/{_account_id(ad_account_id)}/adimages",
        data={"access_token": access_token},
        files={filename: (filename, image_bytes)},
        timeout=60.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    images = body.get("images") or {}
    image = images.get(filename) or next(iter(images.values()), {})
    return {"hash": image.get("hash"), "url": image.get("url")}


def create_ad_creative(
    access_token: str,
    ad_account_id: str,
    *,
    page_id: str,
    name: str,
    primary_text: str,
    destination_url: str,
    headline: Optional[str] = None,
    description: Optional[str] = None,
    call_to_action: str = "LEARN_MORE",
    image_hash: Optional[str] = None,
    video_id: Optional[str] = None,
) -> dict:
    if video_id and not image_hash:
        # object_story_spec.video_data is a materially different shape
        # (needs a thumbnail image plus video-specific CTA nesting) --
        # not implemented yet. The plan's asset picker may pass either
        # type; the image path is this pass's priority.
        raise NotImplementedError("video ad creatives not yet implemented")

    link_data: dict = {
        "message": primary_text,
        "link": destination_url,
        "call_to_action": {"type": call_to_action, "value": {"link": destination_url}},
    }
    if headline:
        link_data["name"] = headline
    if description:
        link_data["description"] = description
    if image_hash:
        link_data["image_hash"] = image_hash

    params = {
        "access_token": access_token,
        "name": name,
        "object_story_spec": {"page_id": page_id, "link_data": link_data},
    }
    return _post(f"{GRAPH_BASE}/{_account_id(ad_account_id)}/adcreatives", _encode(params))


def create_ad(access_token: str, ad_account_id: str, *, adset_id: str, name: str, creative_id: str) -> dict:
    params = {
        "access_token": access_token,
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": "PAUSED",
    }
    return _post(f"{GRAPH_BASE}/{_account_id(ad_account_id)}/ads", _encode(params))


def update_ad(access_token: str, platform_ad_id: str, **fields) -> dict:
    return _post(f"{GRAPH_BASE}/{platform_ad_id}", _encode({"access_token": access_token, **fields}))


# ---------------------------------------------------------------------------
# Targeting pickers
# ---------------------------------------------------------------------------
def search_interests(access_token: str, query: str) -> list[dict]:
    resp = _get(f"{GRAPH_BASE}/search", {
        "access_token": access_token,
        "type": "adinterest",
        "q": query,
    })
    return resp.get("data", [])


def list_custom_audiences(access_token: str, ad_account_id: str) -> list[dict]:
    resp = _get(f"{GRAPH_BASE}/{_account_id(ad_account_id)}/customaudiences", {
        "access_token": access_token,
        "fields": "id,name,subtype,approximate_count_lower_bound",
    })
    return resp.get("data", [])


# ---------------------------------------------------------------------------
# Status sync-back
# ---------------------------------------------------------------------------
def fetch_campaign_status(access_token: str, platform_campaign_id: str) -> dict:
    status_resp = _get(f"{GRAPH_BASE}/{platform_campaign_id}", {
        "access_token": access_token,
        "fields": "status,effective_status,daily_budget,lifetime_budget",
    })
    spend = 0
    try:
        insights_resp = _get(f"{GRAPH_BASE}/{platform_campaign_id}/insights", {
            "access_token": access_token,
            "fields": "spend",
        })
        rows = insights_resp.get("data", [])
        if rows:
            spend = float(rows[0].get("spend") or 0)
    except Exception:  # noqa: BLE001
        pass  # a campaign with zero delivery yet 400s the insights edge

    return {
        "status": status_resp.get("status"),
        "effective_status": status_resp.get("effective_status"),
        "daily_budget": status_resp.get("daily_budget"),
        "lifetime_budget": status_resp.get("lifetime_budget"),
        "spend": spend,
    }

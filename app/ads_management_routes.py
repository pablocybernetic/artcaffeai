"""
ads_management_routes.py
-------------------------
Ads management (Meta first) — create/edit real Meta campaigns, ad sets
and ads from our own UI. ad_campaigns/ad_sets/ads are a local mirror of
Meta's own objects (same pattern as the Locations module mirrors Google
Places): creates/status/budget edits push to Meta here; ads_status_scheduler.py
pulls live status/spend back on a timer.

Prefix: /ads
Auth:   X-Api-Key header (require_api_key, module-local copy — every
        route file in this app defines its own, not a shared import).

Every campaign/ad-set/ad this router creates is forced PAUSED on Meta
regardless of what the request contains — the actual guardrail lives in
connectors/meta_ads_management_connector.py's create_* functions, which
hardcode status="PAUSED" and never accept it as a parameter. This file
never passes a client-supplied status into a create call.

No DELETE endpoints — PATCH .../status=archived instead, matching
Meta's own UI convention.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/ads", dependencies=[Depends(require_api_key)])

_STATUS_TO_META = {"active": "ACTIVE", "paused": "PAUSED", "archived": "ARCHIVED"}
_GENDER_TO_META = {"male": 1, "female": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_cents(amount: Optional[float]) -> Optional[int]:
    return int(round(amount * 100)) if amount is not None else None


def _build_meta_targeting(t: dict) -> dict:
    """Translates our local ad_sets.targeting shape (see 028_ads_management.sql
    and the frontend's NewCampaignSheet) into Meta's actual targeting wire
    format for POST .../adsets — the two are NOT the same shape and Meta
    would reject or silently mishandle the local one if forwarded as-is.

    Deliberately conservative: fields our UI can't yet populate with the
    reference IDs Meta actually requires (region keys, custom-location
    radii, numeric locale IDs — none of these have a search/lookup endpoint
    built yet) are dropped rather than forwarded as free text. Countries,
    age, gender, interests/custom audiences (which already carry Meta's own
    IDs from the interest-search and list-audiences endpoints), and
    placements are the only pieces with a reliable path to Meta's format
    today.
    """
    t = t or {}
    geo = t.get("geo") or {}
    countries = geo.get("countries") or []
    meta_targeting: dict = {}
    if countries:
        meta_targeting["geo_locations"] = {"countries": countries}

    if t.get("age_min") is not None:
        meta_targeting["age_min"] = t["age_min"]
    if t.get("age_max") is not None:
        meta_targeting["age_max"] = t["age_max"]

    genders = [_GENDER_TO_META[g] for g in (t.get("genders") or []) if g in _GENDER_TO_META]
    if genders:
        meta_targeting["genders"] = genders

    interests = [i for i in (t.get("interests") or []) if i.get("id")]
    if interests:
        meta_targeting["flexible_spec"] = [
            {"interests": [{"id": i["id"], "name": i.get("name")} for i in interests]}
        ]

    audience_ids = [a["id"] for a in (t.get("custom_audiences") or []) if a.get("id")]
    audience_ids += [a["id"] for a in (t.get("lookalike_audiences") or []) if a.get("id")]
    if audience_ids:
        meta_targeting["custom_audiences"] = [{"id": aid} for aid in audience_ids]

    placements = t.get("placements") or {}
    if placements.get("mode") == "manual":
        fb = placements.get("facebook_positions") or []
        ig = placements.get("instagram_positions") or []
        platforms = []
        if fb:
            platforms.append("facebook")
            meta_targeting["facebook_positions"] = fb
        if ig:
            platforms.append("instagram")
            meta_targeting["instagram_positions"] = ig
        if platforms:
            meta_targeting["publisher_platforms"] = platforms
    # mode == "advantage" (the default): omit placement fields entirely so
    # Meta auto-selects — that's how Advantage+ automatic placements work.

    return meta_targeting


def _get_creds(concept_id: Optional[str]) -> dict:
    q = sb.table("platform_credentials").select("*").eq("platform", "meta").eq("is_active", True)
    q = q.eq("concept_id", concept_id) if concept_id else q.is_("concept_id", "null")
    res = q.limit(1).execute()
    creds = (res.data or [None])[0] or {}
    if not creds.get("access_token") or not creds.get("ad_account_id"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Meta access token or Ad Account ID not configured — add both in "
                "Settings → Social publishing → Instagram & Facebook"
            ),
        )
    return creds


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class AdIn(BaseModel):
    name: str
    asset_id: Optional[str] = None
    primary_text: Optional[str] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    call_to_action: str = "LEARN_MORE"
    destination_url: Optional[str] = None


class AdSetIn(BaseModel):
    name: str
    budget_amount: Optional[float] = None
    budget_type: Optional[str] = None  # 'daily' | 'lifetime' — only used when the campaign isn't using CBO
    billing_event: str = "IMPRESSIONS"
    optimization_goal: str
    targeting: dict = Field(default_factory=dict)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    ads: list[AdIn] = Field(default_factory=list)


class AdCampaignIn(BaseModel):
    concept_id: str
    name: str
    objective: str
    special_ad_categories: list = Field(default_factory=list)
    buying_type: str = "AUCTION"
    budget_amount: Optional[float] = None
    budget_type: Optional[str] = None  # 'daily' | 'lifetime'
    is_campaign_budget_optimization: bool = True
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    created_by: Optional[str] = None
    ad_sets: list[AdSetIn] = Field(default_factory=list)


class CampaignUpdate(BaseModel):
    status: Optional[str] = None  # 'active' | 'paused' | 'archived'
    budget_amount: Optional[float] = None
    name: Optional[str] = None


class AdSetUpdate(BaseModel):
    status: Optional[str] = None
    budget_amount: Optional[float] = None
    name: Optional[str] = None
    targeting: Optional[dict] = None


class AdUpdate(BaseModel):
    status: Optional[str] = None
    name: Optional[str] = None
    primary_text: Optional[str] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    call_to_action: Optional[str] = None
    destination_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
@router.get("/campaigns")
def list_campaigns(concept_id: str):
    res = (
        sb.table("ad_campaigns")
        .select("*, ad_sets(*, ads(*))")
        .eq("concept_id", concept_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"ok": True, "campaigns": res.data or []}


@router.post("/campaigns")
def create_campaign_endpoint(body: AdCampaignIn):
    from connectors.meta_ads_management_connector import (  # noqa: PLC0415
        create_ad,
        create_ad_creative,
        create_ad_set,
        create_campaign,
        upload_creative_image,
    )

    creds = _get_creds(body.concept_id)
    access_token = creds["access_token"]
    ad_account_id = creds["ad_account_id"]
    page_id = creds.get("page_id")

    campaign_row: dict[str, Any] = {
        "concept_id": body.concept_id,
        "platform": "meta",
        "name": body.name,
        "objective": body.objective,
        "status": "paused",
        "special_ad_categories": body.special_ad_categories,
        "buying_type": body.buying_type,
        "budget_amount": body.budget_amount,
        "budget_type": body.budget_type,
        "is_campaign_budget_optimization": body.is_campaign_budget_optimization,
        "start_time": body.start_time,
        "end_time": body.end_time,
        "created_by": body.created_by,
    }
    campaign_row = {k: v for k, v in campaign_row.items() if v is not None}
    camp_res = sb.table("ad_campaigns").insert(campaign_row).execute()
    campaign = camp_res.data[0]
    campaign_id = campaign["id"]

    had_error = False
    cbo = body.is_campaign_budget_optimization

    try:
        meta_campaign = create_campaign(
            access_token,
            ad_account_id,
            name=body.name,
            objective=body.objective,
            special_ad_categories=body.special_ad_categories,
            budget_amount_cents=_to_cents(body.budget_amount) if cbo else None,
            budget_type=body.budget_type if cbo else None,
            buying_type=body.buying_type,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:500]
        sb.table("ad_campaigns").update({"last_sync_error": err}).eq("id", campaign_id).execute()
        campaign["last_sync_error"] = err
        return {"ok": True, "partial_failure": True, "campaign": campaign, "ad_sets": []}

    platform_campaign_id = meta_campaign.get("id")
    sb.table("ad_campaigns").update({
        "platform_campaign_id": platform_campaign_id,
        "last_synced_at": _now(),
        "last_sync_error": None,
    }).eq("id", campaign_id).execute()
    campaign["platform_campaign_id"] = platform_campaign_id

    ad_sets_result: list[dict] = []
    for ad_set_in in body.ad_sets:
        adset_row: dict[str, Any] = {
            "campaign_id": campaign_id,
            "name": ad_set_in.name,
            "status": "paused",
            "budget_amount": ad_set_in.budget_amount,
            "budget_type": ad_set_in.budget_type,
            "billing_event": ad_set_in.billing_event,
            "optimization_goal": ad_set_in.optimization_goal,
            "targeting": ad_set_in.targeting,
            "start_time": ad_set_in.start_time,
            "end_time": ad_set_in.end_time,
        }
        adset_row = {k: v for k, v in adset_row.items() if v is not None}
        adset_res = sb.table("ad_sets").insert(adset_row).execute()
        adset = adset_res.data[0]
        adset_id = adset["id"]

        try:
            meta_adset = create_ad_set(
                access_token,
                ad_account_id,
                campaign_id=platform_campaign_id,
                name=ad_set_in.name,
                targeting=_build_meta_targeting(ad_set_in.targeting),
                optimization_goal=ad_set_in.optimization_goal,
                billing_event=ad_set_in.billing_event,
                budget_amount_cents=_to_cents(ad_set_in.budget_amount) if not cbo else None,
                budget_type=ad_set_in.budget_type if not cbo else None,
                start_time=ad_set_in.start_time,
                end_time=ad_set_in.end_time,
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:500]
            sb.table("ad_sets").update({"last_sync_error": err}).eq("id", adset_id).execute()
            sb.table("ad_campaigns").update({
                "last_sync_error": f"ad set '{ad_set_in.name}' failed: {err}",
            }).eq("id", campaign_id).execute()
            adset["last_sync_error"] = err
            adset["ads"] = []
            ad_sets_result.append(adset)
            had_error = True
            break  # stop processing further ad sets for this campaign

        platform_adset_id = meta_adset.get("id")
        sb.table("ad_sets").update({
            "platform_adset_id": platform_adset_id,
            "last_synced_at": _now(),
            "last_sync_error": None,
        }).eq("id", adset_id).execute()
        adset["platform_adset_id"] = platform_adset_id

        ads_result: list[dict] = []
        adset_chain_failed = False
        for ad_in in ad_set_in.ads:
            ad_row: dict[str, Any] = {
                "ad_set_id": adset_id,
                "name": ad_in.name,
                "status": "paused",
                "asset_id": ad_in.asset_id,
                "primary_text": ad_in.primary_text,
                "headline": ad_in.headline,
                "description": ad_in.description,
                "call_to_action": ad_in.call_to_action,
                "destination_url": ad_in.destination_url,
            }
            ad_row = {k: v for k, v in ad_row.items() if v is not None}
            ad_res = sb.table("ads").insert(ad_row).execute()
            ad = ad_res.data[0]
            ad_id = ad["id"]

            try:
                if not ad_in.asset_id:
                    raise RuntimeError("ad has no asset_id — an image is required to create a Meta ad creative")
                if not page_id:
                    raise RuntimeError(
                        "Meta Page ID not configured — add it in Settings → Social publishing → Instagram & Facebook"
                    )
                asset_res = sb.table("assets").select("public_url").eq("id", ad_in.asset_id).maybe_single().execute()
                public_url = (asset_res.data if asset_res else {}).get("public_url") if asset_res else None
                if not public_url:
                    raise RuntimeError(f"asset {ad_in.asset_id} has no public_url")

                img_resp = httpx.get(public_url, timeout=30.0)
                if not img_resp.is_success:
                    raise RuntimeError(f"could not download asset image: HTTP {img_resp.status_code}")

                uploaded = upload_creative_image(access_token, ad_account_id, img_resp.content, f"{ad_in.asset_id}.jpg")
                image_hash = uploaded.get("hash")
                if not image_hash:
                    raise RuntimeError("Meta did not return an image hash for the uploaded creative image")

                creative = create_ad_creative(
                    access_token,
                    ad_account_id,
                    page_id=page_id,
                    name=f"{ad_in.name} creative",
                    primary_text=ad_in.primary_text or "",
                    headline=ad_in.headline,
                    description=ad_in.description,
                    call_to_action=ad_in.call_to_action,
                    destination_url=ad_in.destination_url or "",
                    image_hash=image_hash,
                )
                platform_creative_id = creative.get("id")

                meta_ad = create_ad(
                    access_token,
                    ad_account_id,
                    adset_id=platform_adset_id,
                    name=ad_in.name,
                    creative_id=platform_creative_id,
                )
                platform_ad_id = meta_ad.get("id")

                sb.table("ads").update({
                    "platform_ad_id": platform_ad_id,
                    "platform_creative_id": platform_creative_id,
                    "last_synced_at": _now(),
                    "last_sync_error": None,
                }).eq("id", ad_id).execute()
                ad["platform_ad_id"] = platform_ad_id
                ad["platform_creative_id"] = platform_creative_id
                ads_result.append(ad)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:500]
                sb.table("ads").update({"last_sync_error": err}).eq("id", ad_id).execute()
                sb.table("ad_campaigns").update({
                    "last_sync_error": f"ad '{ad_in.name}' failed: {err}",
                }).eq("id", campaign_id).execute()
                ad["last_sync_error"] = err
                ads_result.append(ad)
                had_error = True
                adset_chain_failed = True
                break  # stop processing further ads for this ad set

        adset["ads"] = ads_result
        ad_sets_result.append(adset)
        if adset_chain_failed:
            break  # stop processing further ad sets for this campaign

    return {"ok": True, "partial_failure": had_error, "campaign": campaign, "ad_sets": ad_sets_result}


@router.patch("/campaigns/{campaign_id}")
def update_campaign_endpoint(campaign_id: str, body: CampaignUpdate):
    from connectors.meta_ads_management_connector import update_campaign  # noqa: PLC0415

    camp_res = sb.table("ad_campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    if not camp_res or not camp_res.data:
        raise HTTPException(404, "Campaign not found")
    campaign = camp_res.data
    if not campaign.get("platform_campaign_id"):
        raise HTTPException(400, "Campaign was never successfully created on Meta — nothing to update remotely")
    if body.status is not None and body.status not in _STATUS_TO_META:
        raise HTTPException(400, f"status must be one of {list(_STATUS_TO_META)}")

    creds = _get_creds(campaign.get("concept_id"))

    meta_fields: dict[str, Any] = {}
    if body.status is not None:
        meta_fields["status"] = _STATUS_TO_META[body.status]
    if body.name is not None:
        meta_fields["name"] = body.name
    if body.budget_amount is not None:
        key = "lifetime_budget" if campaign.get("budget_type") == "lifetime" else "daily_budget"
        meta_fields[key] = _to_cents(body.budget_amount)

    if meta_fields:
        try:
            update_campaign(creds["access_token"], campaign["platform_campaign_id"], **meta_fields)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Meta update failed: {exc}") from exc

    local_update: dict[str, Any] = {"updated_at": _now()}
    if body.status is not None:
        local_update["status"] = body.status
    if body.name is not None:
        local_update["name"] = body.name
    if body.budget_amount is not None:
        local_update["budget_amount"] = body.budget_amount

    res = sb.table("ad_campaigns").update(local_update).eq("id", campaign_id).execute()
    return {"ok": True, "campaign": res.data[0]}


# ---------------------------------------------------------------------------
# Ad sets
# ---------------------------------------------------------------------------
@router.patch("/adsets/{ad_set_id}")
def update_ad_set_endpoint(ad_set_id: str, body: AdSetUpdate):
    from connectors.meta_ads_management_connector import update_ad_set  # noqa: PLC0415

    adset_res = sb.table("ad_sets").select("*").eq("id", ad_set_id).maybe_single().execute()
    if not adset_res or not adset_res.data:
        raise HTTPException(404, "Ad set not found")
    ad_set = adset_res.data
    if not ad_set.get("platform_adset_id"):
        raise HTTPException(400, "Ad set was never successfully created on Meta — nothing to update remotely")
    if body.status is not None and body.status not in _STATUS_TO_META:
        raise HTTPException(400, f"status must be one of {list(_STATUS_TO_META)}")

    campaign_res = sb.table("ad_campaigns").select("concept_id").eq("id", ad_set["campaign_id"]).maybe_single().execute()
    concept_id = (campaign_res.data or {}).get("concept_id") if campaign_res else None
    creds = _get_creds(concept_id)

    meta_fields: dict[str, Any] = {}
    if body.status is not None:
        meta_fields["status"] = _STATUS_TO_META[body.status]
    if body.name is not None:
        meta_fields["name"] = body.name
    if body.targeting is not None:
        meta_fields["targeting"] = _build_meta_targeting(body.targeting)
    if body.budget_amount is not None:
        key = "lifetime_budget" if ad_set.get("budget_type") == "lifetime" else "daily_budget"
        meta_fields[key] = _to_cents(body.budget_amount)

    if meta_fields:
        try:
            update_ad_set(creds["access_token"], ad_set["platform_adset_id"], **meta_fields)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Meta update failed: {exc}") from exc

    local_update: dict[str, Any] = {"updated_at": _now()}
    if body.status is not None:
        local_update["status"] = body.status
    if body.name is not None:
        local_update["name"] = body.name
    if body.targeting is not None:
        local_update["targeting"] = body.targeting
    if body.budget_amount is not None:
        local_update["budget_amount"] = body.budget_amount

    res = sb.table("ad_sets").update(local_update).eq("id", ad_set_id).execute()
    return {"ok": True, "ad_set": res.data[0]}


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------
@router.patch("/ads/{ad_id}")
def update_ad_endpoint(ad_id: str, body: AdUpdate):
    from connectors.meta_ads_management_connector import update_ad  # noqa: PLC0415

    if any([body.primary_text, body.headline, body.description, body.call_to_action, body.destination_url]):
        # Meta ad creatives are largely immutable once created — changing
        # primary_text/headline/etc requires creating a NEW creative and
        # reattaching it to the ad, not editing in place. Only the clearly
        # safe fields (status, name) are supported here.
        raise HTTPException(
            501,
            "creative content edits require creating a new creative — not yet implemented, only status changes are supported",
        )

    ad_res = sb.table("ads").select("*").eq("id", ad_id).maybe_single().execute()
    if not ad_res or not ad_res.data:
        raise HTTPException(404, "Ad not found")
    ad = ad_res.data
    if not ad.get("platform_ad_id"):
        raise HTTPException(400, "Ad was never successfully created on Meta — nothing to update remotely")
    if body.status is not None and body.status not in _STATUS_TO_META:
        raise HTTPException(400, f"status must be one of {list(_STATUS_TO_META)}")

    adset_res = sb.table("ad_sets").select("campaign_id").eq("id", ad["ad_set_id"]).maybe_single().execute()
    campaign_id = (adset_res.data or {}).get("campaign_id") if adset_res else None
    campaign_res = sb.table("ad_campaigns").select("concept_id").eq("id", campaign_id).maybe_single().execute()
    concept_id = (campaign_res.data or {}).get("concept_id") if campaign_res else None
    creds = _get_creds(concept_id)

    meta_fields: dict[str, Any] = {}
    if body.status is not None:
        meta_fields["status"] = _STATUS_TO_META[body.status]
    if body.name is not None:
        meta_fields["name"] = body.name

    if meta_fields:
        try:
            update_ad(creds["access_token"], ad["platform_ad_id"], **meta_fields)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Meta update failed: {exc}") from exc

    local_update: dict[str, Any] = {"updated_at": _now()}
    if body.status is not None:
        local_update["status"] = body.status
    if body.name is not None:
        local_update["name"] = body.name

    res = sb.table("ads").update(local_update).eq("id", ad_id).execute()
    return {"ok": True, "ad": res.data[0]}


# ---------------------------------------------------------------------------
# Targeting pickers
# ---------------------------------------------------------------------------
@router.get("/meta/interests")
def search_interests_endpoint(q: str, concept_id: str):
    from connectors.meta_ads_management_connector import search_interests  # noqa: PLC0415

    creds = _get_creds(concept_id)
    try:
        results = search_interests(creds["access_token"], q)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "interests": results}


@router.get("/meta/custom-audiences")
def list_custom_audiences_endpoint(concept_id: str):
    from connectors.meta_ads_management_connector import list_custom_audiences  # noqa: PLC0415

    creds = _get_creds(concept_id)
    try:
        results = list_custom_audiences(creds["access_token"], creds["ad_account_id"])
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "audiences": results}

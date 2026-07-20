"""
ads_routes.py
-------------
FastAPI router for direct Meta Ads and Google Ads data sync.

Prefix: /data/ads
Auth:   X-Api-Key header

Endpoints:
  POST /data/ads/meta/sync    — pull Meta Ads Insights API → platform_data_snapshots
  POST /data/ads/google/sync  — pull Google Ads reporting API → platform_data_snapshots
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
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


router = APIRouter(
    prefix="/data/ads",
    dependencies=[Depends(require_api_key)],
)


def _get_creds(platform: str) -> dict:
    """Return the credential row as a flat dict (top-level columns, not credentials_json)."""
    res = (
        sb.table("platform_credentials")
        .select("*")
        .eq("platform", platform)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return {}
    return res.data[0] or {}


class AdsSyncRequest(BaseModel):
    concept_id: str
    date_range_days: int = 7


# ---------------------------------------------------------------------------
# Meta Ads sync
# ---------------------------------------------------------------------------
@router.post("/meta/sync")
def sync_meta_ads_endpoint(req: AdsSyncRequest):
    """Pull Meta Ads Insights API data and store as a platform_data_snapshot."""
    from connectors.meta_ads_connector import sync_meta_ads  # noqa: PLC0415

    creds = _get_creds("meta")
    access_token = creds.get("access_token")
    ad_account_id = creds.get("ad_account_id")

    if not access_token:
        raise HTTPException(status_code=400, detail="Meta credentials not configured (missing access_token)")
    if not ad_account_id:
        raise HTTPException(
            status_code=400,
            detail="Meta Ad Account ID not configured — add it in Settings → Social publishing → Instagram & Facebook",
        )

    try:
        summary = sync_meta_ads(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            ad_account_id=ad_account_id,
            date_range_days=req.date_range_days,
        )
        return {
            "ok": True,
            "platform": "meta_ads",
            "concept_id": req.concept_id,
            "snapshot": {
                "totals": summary["totals"],
                "campaigns_count": len(summary.get("campaigns", [])),
                "ad_sets_count": len(summary.get("ad_sets", [])),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meta Ads sync failed: {e}")


# ---------------------------------------------------------------------------
# Google Ads sync
# ---------------------------------------------------------------------------
@router.post("/google/sync")
def sync_google_ads_endpoint(req: AdsSyncRequest):
    """Pull Google Ads reporting API data and store as a platform_data_snapshot."""
    from connectors.google_ads_connector import sync_google_ads  # noqa: PLC0415

    creds = _get_creds("google_ads")
    access_token = creds.get("access_token")
    developer_token = creds.get("developer_token")
    customer_id = creds.get("customer_id")

    if not access_token or not developer_token or not customer_id:
        raise HTTPException(
            status_code=400,
            detail="Google Ads credentials incomplete — access_token, developer_token, and customer_id are required",
        )

    try:
        summary = sync_google_ads(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            developer_token=developer_token,
            customer_id=customer_id,
            date_range_days=req.date_range_days,
        )
        return {
            "ok": True,
            "platform": "google_ads",
            "concept_id": req.concept_id,
            "snapshot": {
                "totals": summary["totals"],
                "campaigns_count": len(summary.get("campaigns", [])),
                "ad_groups_count": len(summary.get("ad_groups", [])),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Ads sync failed: {e}")


# ---------------------------------------------------------------------------
# Meta Organic (Instagram + Facebook Page insights) sync
# ---------------------------------------------------------------------------
@router.post("/meta/organic/sync")
def sync_meta_organic_endpoint(req: AdsSyncRequest):
    """Pull Instagram Business organic metrics and store as a platform_data_snapshot."""
    from connectors.meta_organic_connector import sync_meta_organic  # noqa: PLC0415

    creds = _get_creds("meta")
    access_token = creds.get("access_token")
    # ig_user_id is the Instagram Business Account ID (stored by publishing_routes save_credentials)
    instagram_account_id = creds.get("ig_user_id") or creds.get("instagram_account_id")
    page_id = creds.get("page_id") or ""

    if not access_token:
        raise HTTPException(status_code=400, detail="Meta access_token not configured — add it in Settings → Social publishing")
    if not instagram_account_id:
        raise HTTPException(status_code=400, detail="Instagram Account ID not configured — add it in Settings → Social publishing → Instagram & Facebook")

    try:
        summary = sync_meta_organic(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            instagram_account_id=instagram_account_id,
            page_id=page_id,
            date_range_days=req.date_range_days,
        )
        ig = summary.get("instagram", {})
        totals = ig.get("totals", {})
        return {
            "ok": True,
            "platform": "meta_organic",
            "concept_id": req.concept_id,
            "snapshot": {
                "followers": totals.get("followers"),
                "posts_in_range": totals.get("posts_in_range"),
                "likes": totals.get("likes"),
                "comments": totals.get("comments"),
                "reach": totals.get("reach"),
                "engagement_rate_pct": totals.get("engagement_rate_pct"),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meta organic sync failed: {e}")

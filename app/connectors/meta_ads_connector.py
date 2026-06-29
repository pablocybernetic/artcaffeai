"""
meta_ads_connector.py
---------------------
Pulls campaign and ad set performance from Meta Marketing API v21.0.
Stores results in platform_data_snapshots with platform="meta_ads".

Credentials required (from platform_credentials table, platform="meta"):
  access_token  — long-lived token with ads_read + read_insights scope
  ad_account_id — Meta Ad Account ID (with or without "act_" prefix)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx

from supabase import Client

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _get(url: str, params: dict[str, str], timeout: float = 30.0) -> dict:
    resp = httpx.get(url, params=params, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"Meta Ads API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def sync_meta_ads(
    *,
    sb: Client,
    concept_id: str,
    access_token: str,
    ad_account_id: str,
    date_range_days: int = 7,
) -> dict[str, Any]:
    """
    Pull Meta Ads Insights for the last N days at campaign and ad-set level.
    Upserts a snapshot row and returns the summary dict.
    """
    end_dt = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=date_range_days - 1)

    account_id = ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
    time_range = json.dumps({"since": start_dt.isoformat(), "until": end_dt.isoformat()})
    base_url = f"{GRAPH_BASE}/{account_id}/insights"

    # Campaign-level
    camp_resp = _get(base_url, {
        "access_token": access_token,
        "level": "campaign",
        "fields": "campaign_id,campaign_name,impressions,clicks,spend,ctr,cpm,reach,frequency",
        "time_range": time_range,
        "limit": "50",
    })
    campaigns: list[dict] = camp_resp.get("data", [])

    # Ad set-level
    adset_resp = _get(base_url, {
        "access_token": access_token,
        "level": "adset",
        "fields": "adset_id,adset_name,campaign_name,impressions,clicks,spend,ctr,cpm",
        "time_range": time_range,
        "limit": "100",
    })
    ad_sets: list[dict] = adset_resp.get("data", [])

    total_impressions = sum(int(c.get("impressions") or 0) for c in campaigns)
    total_clicks = sum(int(c.get("clicks") or 0) for c in campaigns)
    total_spend = sum(float(c.get("spend") or 0) for c in campaigns)

    summary: dict[str, Any] = {
        "source": "meta_ads_api",
        "ad_account_id": account_id,
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "totals": {
            "impressions": total_impressions,
            "clicks": total_clicks,
            "spend": round(total_spend, 2),
            "ctr": round(total_clicks / total_impressions, 4) if total_impressions else 0,
        },
        "campaigns": campaigns,
        "ad_sets": ad_sets,
    }

    today = date.today().isoformat()
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "meta_ads",
            "snapshot_date": today,
            "summary_json": summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()

    print(
        f"[meta_ads] synced concept={concept_id[:8]} "
        f"campaigns={len(campaigns)} ad_sets={len(ad_sets)} "
        f"spend={total_spend:.2f} impressions={total_impressions}",
        flush=True,
    )
    return summary

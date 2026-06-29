"""
google_ads_connector.py
-----------------------
Pulls campaign and ad group performance from Google Ads API v18.
Stores results in platform_data_snapshots with platform="google_ads".

Credentials required (from platform_credentials table, platform="google_ads"):
  access_token    — OAuth2 bearer token (scope: https://www.googleapis.com/auth/adwords)
  developer_token — Google Ads developer token
  customer_id     — Google Ads customer ID (with or without dashes)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from supabase import Client

GADS_BASE = "https://googleads.googleapis.com/v18"


def _search_stream(
    customer_id: str,
    query: str,
    access_token: str,
    developer_token: str,
) -> list[dict]:
    """Execute a GAQL query and return all result rows."""
    clean_cid = customer_id.replace("-", "")
    url = f"{GADS_BASE}/customers/{clean_cid}/googleAds:searchStream"
    resp = httpx.post(
        url,
        json={"query": query},
        headers={
            "Authorization": f"Bearer {access_token}",
            "developer-token": developer_token,
        },
        timeout=30.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"Google Ads API error {resp.status_code}: {resp.text[:300]}")

    # searchStream returns a JSON array of batch objects
    batches = resp.json()
    if not isinstance(batches, list):
        batches = [batches]
    rows: list[dict] = []
    for batch in batches:
        rows.extend(batch.get("results", []))
    return rows


def _micros_to_usd(micros: Any) -> float:
    return round(int(micros or 0) / 1_000_000, 2)


def sync_google_ads(
    *,
    sb: Client,
    concept_id: str,
    access_token: str,
    developer_token: str,
    customer_id: str,
    date_range_days: int = 7,
) -> dict[str, Any]:
    """
    Pull Google Ads campaign + ad group metrics for the last N days.
    Upserts a snapshot row and returns the summary dict.
    """
    end_dt = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=date_range_days - 1)
    start_s = start_dt.isoformat()
    end_s = end_dt.isoformat()

    campaign_query = f"""
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.ctr,
  metrics.average_cpc,
  metrics.conversions
FROM campaign
WHERE segments.date BETWEEN '{start_s}' AND '{end_s}'
  AND campaign.status != 'REMOVED'
ORDER BY metrics.cost_micros DESC
LIMIT 50
"""

    adgroup_query = f"""
SELECT
  ad_group.id,
  ad_group.name,
  campaign.name,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.ctr,
  metrics.conversions
FROM ad_group
WHERE segments.date BETWEEN '{start_s}' AND '{end_s}'
  AND campaign.status != 'REMOVED'
ORDER BY metrics.cost_micros DESC
LIMIT 100
"""

    camp_rows = _search_stream(customer_id, campaign_query, access_token, developer_token)
    adgroup_rows = _search_stream(customer_id, adgroup_query, access_token, developer_token)

    campaigns: list[dict] = []
    for r in camp_rows:
        camp = r.get("campaign", {})
        m = r.get("metrics", {})
        campaigns.append({
            "campaign_id": camp.get("id"),
            "campaign_name": camp.get("name"),
            "status": camp.get("status"),
            "impressions": int(m.get("impressions") or 0),
            "clicks": int(m.get("clicks") or 0),
            "spend": _micros_to_usd(m.get("costMicros")),
            "ctr": round(float(m.get("ctr") or 0), 4),
            "avg_cpc": _micros_to_usd(m.get("averageCpc")),
            "conversions": float(m.get("conversions") or 0),
        })

    ad_groups: list[dict] = []
    for r in adgroup_rows:
        ag = r.get("adGroup", {})
        camp = r.get("campaign", {})
        m = r.get("metrics", {})
        ad_groups.append({
            "ad_group_id": ag.get("id"),
            "ad_group_name": ag.get("name"),
            "campaign_name": camp.get("name"),
            "impressions": int(m.get("impressions") or 0),
            "clicks": int(m.get("clicks") or 0),
            "spend": _micros_to_usd(m.get("costMicros")),
            "ctr": round(float(m.get("ctr") or 0), 4),
            "conversions": float(m.get("conversions") or 0),
        })

    total_impressions = sum(c["impressions"] for c in campaigns)
    total_clicks = sum(c["clicks"] for c in campaigns)
    total_spend = sum(c["spend"] for c in campaigns)

    summary: dict[str, Any] = {
        "source": "google_ads_api",
        "customer_id": customer_id,
        "range": {"start": start_s, "end": end_s},
        "totals": {
            "impressions": total_impressions,
            "clicks": total_clicks,
            "spend": round(total_spend, 2),
            "ctr": round(total_clicks / total_impressions, 4) if total_impressions else 0,
        },
        "campaigns": campaigns,
        "ad_groups": ad_groups,
    }

    today = date.today().isoformat()
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "google_ads",
            "snapshot_date": today,
            "summary_json": summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()

    print(
        f"[google_ads] synced concept={concept_id[:8]} "
        f"campaigns={len(campaigns)} ad_groups={len(ad_groups)} "
        f"spend={total_spend:.2f} impressions={total_impressions}",
        flush=True,
    )
    return summary

"""
meta_organic_connector.py
--------------------------
Pulls Instagram Business organic metrics from Meta Graph API v21.0.
Also fetches Facebook Page follower/engagement data if page_id is provided.

Stores results in platform_data_snapshots with platform="meta_organic".

Credentials required (from platform_credentials table, platform="meta"):
  access_token         — long-lived token with instagram_basic,
                         instagram_manage_insights, pages_read_engagement
  instagram_account_id — Instagram Business Account ID (numeric string)
  page_id              — Facebook Page ID (optional, for FB page metrics)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
from supabase import Client

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _get(url: str, params: dict, timeout: float = 30.0) -> dict:
    resp = httpx.get(url, params=params, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _post_insights(post_id: str, access_token: str, media_type: str) -> dict:
    """Fetch per-post insights — metric set differs by media type."""
    if media_type == "VIDEO":
        metrics = "reach,impressions,saved,shares,plays"
    elif media_type == "REEL":
        metrics = "reach,plays,likes,comments,shares,saved,total_interactions"
    else:
        metrics = "reach,impressions,saved,shares"

    try:
        resp = _get(f"{GRAPH_BASE}/{post_id}/insights", {
            "access_token": access_token,
            "metric": metrics,
        })
        result: dict[str, Any] = {}
        for item in resp.get("data", []):
            vals = item.get("values") or []
            # values list for lifetime metrics contains a single entry
            result[item["name"]] = vals[-1].get("value", 0) if vals else item.get("value", 0)
        return result
    except Exception:  # noqa: BLE001
        return {}


def sync_meta_organic(
    *,
    sb: Client,
    concept_id: str,
    access_token: str,
    instagram_account_id: str,
    page_id: str = "",
    date_range_days: int = 28,
) -> dict[str, Any]:
    """
    Pull Instagram Business + Facebook Page organic metrics.
    Upserts a snapshot row and returns the summary dict.
    """
    end_dt = date.today()
    start_dt = end_dt - timedelta(days=date_range_days - 1)

    # ------------------------------------------------------------------
    # 1. Instagram account info
    # ------------------------------------------------------------------
    account = _get(f"{GRAPH_BASE}/{instagram_account_id}", {
        "access_token": access_token,
        "fields": "followers_count,media_count,name,username,biography",
    })

    # ------------------------------------------------------------------
    # 2. Instagram account-level insights (reach, impressions, profile views)
    # ------------------------------------------------------------------
    ig_insights: dict[str, Any] = {}
    try:
        insights_resp = _get(f"{GRAPH_BASE}/{instagram_account_id}/insights", {
            "access_token": access_token,
            "metric": "impressions,reach,profile_views",
            "period": "day",
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat(),
        })
        for metric in insights_resp.get("data", []):
            vals = metric.get("values") or []
            ig_insights[metric["name"]] = {
                "total": sum(v.get("value", 0) for v in vals),
                "daily": [{"date": v.get("end_time", "")[:10], "value": v.get("value", 0)} for v in vals],
            }
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------------------------------
    # 3. Recent media (posts + reels)
    # ------------------------------------------------------------------
    media_resp = _get(f"{GRAPH_BASE}/{instagram_account_id}/media", {
        "access_token": access_token,
        "fields": "id,caption,media_type,timestamp,like_count,comments_count,media_url,thumbnail_url,permalink",
        "limit": "20",
    })
    raw_posts = media_resp.get("data", [])

    # Filter to date range
    posts: list[dict[str, Any]] = []
    for p in raw_posts:
        ts = p.get("timestamp", "")
        if ts and ts[:10] >= start_dt.isoformat():
            insights = _post_insights(p["id"], access_token, p.get("media_type", "IMAGE"))
            posts.append({**p, "insights": insights})

    # ------------------------------------------------------------------
    # 4. Facebook Page metrics (optional)
    # ------------------------------------------------------------------
    fb_page: dict[str, Any] = {}
    if page_id:
        # Step 4a: Page info (works with user token)
        try:
            page_info = _get(f"{GRAPH_BASE}/{page_id}", {
                "access_token": access_token,
                "fields": "name,followers_count,fan_count",
            })
            fb_page["info"] = page_info
        except Exception as e:
            print(f"[meta_organic] FB page info failed: {e}", flush=True)

        # Step 4b: Try to get a Page Access Token (needed for insights)
        page_token = access_token  # fallback to user token
        try:
            accounts_resp = _get(f"{GRAPH_BASE}/me/accounts", {
                "access_token": access_token,
                "fields": "id,access_token",
            })
            for acct in accounts_resp.get("data", []):
                if str(acct.get("id")) == str(page_id):
                    page_token = acct["access_token"]
                    print(f"[meta_organic] using page access token for page {page_id}", flush=True)
                    break
        except Exception as e:
            print(f"[meta_organic] could not fetch page token: {e}", flush=True)

        # Step 4c: Page insights (page_engaged_users deprecated in v17+; use page_views_total)
        try:
            page_insights_resp = _get(f"{GRAPH_BASE}/{page_id}/insights", {
                "access_token": page_token,
                "metric": "page_impressions,page_post_engagements,page_views_total",
                "period": "day",
                "since": start_dt.isoformat(),
                "until": end_dt.isoformat(),
            })
            page_metrics: dict[str, Any] = {}
            for metric in page_insights_resp.get("data", []):
                vals = metric.get("values") or []
                page_metrics[metric["name"]] = sum(v.get("value", 0) for v in vals)
            fb_page["metrics"] = page_metrics
            print(f"[meta_organic] FB metrics: {list(page_metrics.keys())}", flush=True)
        except Exception as e:
            print(f"[meta_organic] FB page insights failed: {e}", flush=True)
            fb_page["insights_error"] = str(e)

    # ------------------------------------------------------------------
    # 5. Aggregate totals
    # ------------------------------------------------------------------
    total_likes = sum(int(p.get("like_count") or 0) for p in posts)
    total_comments = sum(int(p.get("comments_count") or 0) for p in posts)
    total_reach = sum(int(p.get("insights", {}).get("reach") or 0) for p in posts)
    total_shares = sum(int(p.get("insights", {}).get("shares") or 0) for p in posts)
    total_saves = sum(int(p.get("insights", {}).get("saved") or 0) for p in posts)
    followers = account.get("followers_count", 0)
    engagement_rate = round(
        (total_likes + total_comments) / (followers * len(posts)) * 100, 2
    ) if followers and posts else 0.0

    summary: dict[str, Any] = {
        "source": "meta_organic",
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "instagram": {
            "account": {
                "id": instagram_account_id,
                "name": account.get("name"),
                "username": account.get("username"),
                "followers_count": followers,
                "media_count": account.get("media_count", 0),
            },
            "totals": {
                "followers": followers,
                "posts_in_range": len(posts),
                "likes": total_likes,
                "comments": total_comments,
                "reach": total_reach,
                "shares": total_shares,
                "saves": total_saves,
                "engagement_rate_pct": engagement_rate,
                "impressions": ig_insights.get("impressions", {}).get("total", 0),
                "account_reach": ig_insights.get("reach", {}).get("total", 0),
                "profile_views": ig_insights.get("profile_views", {}).get("total", 0),
            },
            "account_insights": ig_insights,
            "posts": posts,
        },
        "facebook_page": fb_page,
    }

    today = date.today().isoformat()
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "meta_organic",
            "snapshot_date": today,
            "summary_json": summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()

    print(
        f"[meta_organic] concept={concept_id[:8]} "
        f"followers={followers} posts={len(posts)} "
        f"likes={total_likes} comments={total_comments} reach={total_reach}",
        flush=True,
    )
    return summary

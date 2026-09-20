"""
tiktok_organic_connector.py
------------------------------
Pulls TikTok organic metrics (follower count, video list, per-video
view/like/comment/share counts) via the TikTok API v2 Display endpoints.

Stores results in platform_data_snapshots with platform="tiktok_organic",
and individual videos into social_posts (platform="tiktok") — mirrors
meta_organic_connector.py's shape exactly so the Dashboard's existing
"All posts" browser and engagement charts work for TikTok with minimal
changes.

Credentials required (from platform_credentials table, platform="tiktok",
scoped per concept/brand):
  access_token   — expires in 24h; refreshed here just-in-time if expired
  refresh_token  — stored in extra_json, valid ~365 days
  open_id        — TikTok's per-user account identifier, stored in extra_json

Unlike Meta's long-lived Page token, every call here first checks token
freshness and refreshes+persists before hitting the Display API — see
tiktok_auth.ensure_fresh_token(). Every other Meta-mirroring integration
point (tiktok_sync_scheduler.py, publishing_routes.py's tiktok publish
branch) should route through that same helper rather than reading
access_token directly off the row, or it'll intermittently fail once
tokens start expiring.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from supabase import Client

from connectors.tiktok_auth import ensure_fresh_token

API_BASE = "https://open.tiktokapis.com/v2"


def _get(url: str, access_token: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    resp = httpx.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"TikTok API {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    error = data.get("error") or {}
    if error.get("code") and error["code"] != "ok":
        raise RuntimeError(f"TikTok API error {error['code']}: {error.get('message', '')[:300]}")
    return data.get("data") or {}


def _post(url: str, access_token: str, json_body: dict, params: dict | None = None, timeout: float = 30.0) -> dict:
    resp = httpx.post(url, params=params, json=json_body, headers={"Authorization": f"Bearer {access_token}"}, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"TikTok API {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    error = data.get("error") or {}
    if error.get("code") and error["code"] != "ok":
        raise RuntimeError(f"TikTok API error {error['code']}: {error.get('message', '')[:300]}")
    return data.get("data") or {}


def sync_tiktok_organic(
    *,
    sb: Client,
    concept_id: str,
    date_range_days: int = 28,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Pull TikTok account + video organic metrics for one concept/brand.
    Upserts a snapshot row and returns the summary dict. Looks up its own
    credentials (unlike sync_meta_organic, which takes them as args) so it
    can transparently refresh+persist an expired token mid-call."""
    creds_res = (
        sb.table("platform_credentials")
        .select("*")
        .eq("platform", "tiktok")
        .eq("concept_id", concept_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    creds = (creds_res.data or [{}])[0] if creds_res.data else {}
    if not creds.get("access_token"):
        return {"skipped": True, "reason": "no TikTok credentials configured"}

    access_token = ensure_fresh_token(sb, concept_id, creds)

    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    start_dt = end_dt - timedelta(days=date_range_days - 1)

    # 1. Account info (follower/like/video counts)
    account = _get(f"{API_BASE}/user/info/", access_token, {
        "fields": "open_id,display_name,follower_count,following_count,likes_count,video_count",
    })
    user = account.get("user") or {}

    # 2. Video list (up to 50, newest first) + per-video stats in the same call
    # TikTok caps max_count at 20 per call — pagination via the returned
    # cursor would be needed to fetch more, not implemented here since 20
    # most-recent videos is enough for this dashboard's purposes today.
    videos_resp = _post(f"{API_BASE}/video/list/", access_token, {"max_count": 20}, params={
        "fields": "id,title,cover_image_url,share_url,create_time,view_count,like_count,comment_count,share_count",
    })
    raw_videos = videos_resp.get("videos") or []

    all_posts = [
        {
            "id": v["id"],
            "caption": v.get("title"),
            "permalink": v.get("share_url"),
            "thumbnail_url": v.get("cover_image_url"),
            "posted_at": datetime.fromtimestamp(v["create_time"], tz=timezone.utc).isoformat() if v.get("create_time") else None,
            "view_count": int(v.get("view_count") or 0),
            "like_count": int(v.get("like_count") or 0),
            "comment_count": int(v.get("comment_count") or 0),
            "share_count": int(v.get("share_count") or 0),
        }
        for v in raw_videos
    ]

    if all_posts:
        post_rows = [
            {
                "concept_id": concept_id,
                "platform": "tiktok",
                "post_id": p["id"],
                "media_type": "VIDEO",
                "caption": p.get("caption"),
                "permalink": p.get("permalink"),
                "media_url": None,
                "thumbnail_url": p.get("thumbnail_url"),
                "posted_at": p.get("posted_at"),
                "like_count": p["like_count"],
                "comments_count": p["comment_count"],
                "insights": {"view_count": p["view_count"], "share_count": p["share_count"]},
                "synced_at": end_dt.isoformat(),
            }
            for p in all_posts
        ]
        try:
            sb.table("social_posts").upsert(post_rows, on_conflict="concept_id,platform,post_id").execute()
            print(f"[tiktok_organic] upserted {len(post_rows)} videos into social_posts", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[tiktok_organic] social_posts upsert failed: {e}", flush=True)

    posts_in_range = [p for p in all_posts if (p.get("posted_at") or "")[:10] >= start_dt.isoformat()]

    total_views = sum(p["view_count"] for p in posts_in_range)
    total_likes = sum(p["like_count"] for p in posts_in_range)
    total_comments = sum(p["comment_count"] for p in posts_in_range)
    total_shares = sum(p["share_count"] for p in posts_in_range)
    followers = user.get("follower_count", 0)
    engagement_rate = round(
        (total_likes + total_comments) / (followers * len(posts_in_range)) * 100, 2
    ) if followers and posts_in_range else 0.0

    summary: dict[str, Any] = {
        "source": "tiktok_organic",
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "account": {
            "open_id": user.get("open_id"),
            "display_name": user.get("display_name"),
            "followers_count": followers,
            "video_count": user.get("video_count", 0),
            "likes_count": user.get("likes_count", 0),
        },
        "totals": {
            "followers": followers,
            "posts_in_range": len(posts_in_range),
            "views": total_views,
            "likes": total_likes,
            "comments": total_comments,
            "shares": total_shares,
            "engagement_rate_pct": engagement_rate,
        },
        "posts": posts_in_range,
    }

    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "tiktok_organic",
            "snapshot_date": end_dt.isoformat(),
            "summary_json": summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()

    print(
        f"[tiktok_organic] concept={concept_id[:8]} followers={followers} "
        f"videos={len(posts_in_range)} views={total_views} likes={total_likes}",
        flush=True,
    )
    return summary

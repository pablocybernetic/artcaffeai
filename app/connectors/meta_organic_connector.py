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

import mimetypes
import os
from datetime import date, timedelta
from typing import Any

import httpx
from supabase import Client

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# Meta's thumbnail_url/media_url are signed CDN links that expire (see the
# `oe=` param) a few days after being fetched. Once a post ages out of the
# most-recent-N window this connector re-syncs, its URL is never refreshed
# again and eventually 403s -- so every post's media gets downloaded once
# and re-hosted in our own Storage bucket, which never expires.
STORAGE_BUCKET = os.environ.get("ASSETS_BUCKET", "generated-assets")
_REHOSTED_MARKER = "/storage/v1/object/public/"


def _get(url: str, params: dict, timeout: float = 30.0) -> dict:
    resp = httpx.get(url, params=params, timeout=timeout)
    if not resp.is_success:
        raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _is_rehosted(url: str | None) -> bool:
    return bool(url) and _REHOSTED_MARKER in url


def _fetch_existing_media(sb: Client, concept_id: str, platform: str) -> dict[str, dict]:
    """One bulk lookup per sync (not per-post) of what's already stored, so
    already-rehosted posts are never re-downloaded."""
    try:
        res = (
            sb.table("social_posts")
            .select("post_id,media_url,thumbnail_url")
            .eq("concept_id", concept_id)
            .eq("platform", platform)
            .execute()
        )
        return {r["post_id"]: r for r in (res.data or [])}
    except Exception:  # noqa: BLE001
        return {}


def _rehost(sb: Client, url: str | None, storage_path_prefix: str) -> str | None:
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        if not resp.is_success:
            return None
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or "application/octet-stream"
        ext = mimetypes.guess_extension(content_type) or ""
        storage_path = f"{storage_path_prefix}{ext}"
        sb.storage.from_(STORAGE_BUCKET).upload(
            storage_path,
            resp.content,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        return sb.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
    except Exception as e:  # noqa: BLE001
        print(f"[meta_organic] re-host failed for {storage_path_prefix}: {e}", flush=True)
        return None


def _rehost_pair(
    sb: Client,
    existing: dict | None,
    media_url: str | None,
    thumbnail_url: str | None,
    base_path: str,
) -> tuple[str | None, str | None]:
    """Returns (media_url, thumbnail_url) to actually store. Skips
    re-downloading entirely once both are already pointing at our own
    Storage bucket; falls back to Meta's (temporarily valid) URL if a
    download/upload fails, rather than losing the post's media entirely."""
    if existing and _is_rehosted(existing.get("thumbnail_url")) and _is_rehosted(existing.get("media_url")):
        return existing.get("media_url"), existing.get("thumbnail_url")
    new_thumb = _rehost(sb, thumbnail_url, f"{base_path}/thumb") or thumbnail_url
    new_media = _rehost(sb, media_url, f"{base_path}/media") or media_url
    return new_media, new_thumb


def _carousel_cover(media_id: str, access_token: str) -> dict:
    """CAROUSEL_ALBUM nodes never populate media_url/thumbnail_url on the
    parent — the actual images/videos only exist on child items behind the
    /children edge. Fetch the first child to use as the album's cover."""
    try:
        resp = _get(f"{GRAPH_BASE}/{media_id}/children", {
            "access_token": access_token,
            "fields": "media_type,media_url,thumbnail_url",
        })
        children = resp.get("data", [])
        if not children:
            return {}
        first = children[0]
        return {
            "media_url": first.get("media_url"),
            "thumbnail_url": first.get("thumbnail_url") or first.get("media_url"),
        }
    except Exception:  # noqa: BLE001
        return {}


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
    instagram_account_id: str = "",
    page_id: str = "",
    date_range_days: int = 28,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Pull Instagram Business + Facebook Page organic metrics.
    Upserts a snapshot row and returns the summary dict.
    end_date (YYYY-MM-DD) lets the dashboard's date-range filter request a
    window not ending today — defaults to today when omitted.
    """
    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    start_dt = end_dt - timedelta(days=date_range_days - 1)

    # ------------------------------------------------------------------
    # 1-3b. Instagram (optional — concepts with only a Facebook Page
    # configured skip all of this and still get a valid summary below)
    # ------------------------------------------------------------------
    account: dict[str, Any] = {}
    ig_insights: dict[str, Any] = {}
    posts: list[dict[str, Any]] = []
    all_posts: list[dict[str, Any]] = []

    if instagram_account_id:
        # 1. Instagram account info
        account = _get(f"{GRAPH_BASE}/{instagram_account_id}", {
            "access_token": access_token,
            "fields": "followers_count,media_count,name,username,biography",
        })

        # 2. Instagram account-level insights (reach, impressions, profile views)
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

        # 3. Recent media (posts + reels) — fetch up to 50 for DB archiving
        media_resp = _get(f"{GRAPH_BASE}/{instagram_account_id}/media", {
            "access_token": access_token,
            "fields": "id,caption,media_type,timestamp,like_count,comments_count,media_url,thumbnail_url,permalink",
            "limit": "50",
        })
        raw_posts = media_resp.get("data", [])

        # Enrich all posts with per-post insights (stored in DB regardless of date range)
        for p in raw_posts:
            if p.get("media_type") == "CAROUSEL_ALBUM" and not p.get("media_url"):
                p = {**p, **_carousel_cover(p["id"], access_token)}
            insights = _post_insights(p["id"], access_token, p.get("media_type", "IMAGE"))
            all_posts.append({**p, "insights": insights})

        # Filter to date range for dashboard stats
        posts = [
            p for p in all_posts
            if p.get("timestamp", "")[:10] >= start_dt.isoformat()
        ]

        # 3b. Upsert individual posts into social_posts for AI system knowledge
        if all_posts:
            existing_ig = _fetch_existing_media(sb, concept_id, "instagram")
            post_rows = []
            for p in all_posts:
                media_url, thumbnail_url = _rehost_pair(
                    sb,
                    existing_ig.get(p["id"]),
                    p.get("media_url"),
                    p.get("thumbnail_url"),
                    f"synced-posts/{concept_id}/instagram/{p['id']}",
                )
                post_rows.append({
                    "concept_id": concept_id,
                    "platform": "instagram",
                    "post_id": p["id"],
                    "media_type": p.get("media_type"),
                    "caption": p.get("caption"),
                    "permalink": p.get("permalink"),
                    "media_url": media_url,
                    "thumbnail_url": thumbnail_url,
                    "posted_at": p.get("timestamp"),
                    "like_count": int(p.get("like_count") or 0),
                    "comments_count": int(p.get("comments_count") or 0),
                    "insights": p.get("insights", {}),
                    "synced_at": end_dt.isoformat(),
                })
            try:
                sb.table("social_posts").upsert(
                    post_rows,
                    on_conflict="concept_id,platform,post_id",
                ).execute()
                print(f"[meta_organic] upserted {len(post_rows)} posts into social_posts", flush=True)
            except Exception as e:
                print(f"[meta_organic] social_posts upsert failed: {e}", flush=True)

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

        # Step 4c: Page posts — fetch + archive into social_posts (mirrors the
        # Instagram media archiving above), so the Dashboard's "All posts"
        # browser has Facebook history too.
        try:
            fb_posts_resp = _get(f"{GRAPH_BASE}/{page_id}/posts", {
                "access_token": page_token,
                "fields": (
                    "id,message,created_time,permalink_url,full_picture,"
                    "likes.summary(true),comments.summary(true),shares,"
                    "attachments{media_type,media}"
                ),
                "limit": "50",
            })
            fb_raw_posts = fb_posts_resp.get("data", [])
            existing_fb = _fetch_existing_media(sb, concept_id, "facebook")
            fb_post_rows = []
            for p in fb_raw_posts:
                attachment = ((p.get("attachments") or {}).get("data") or [{}])[0]
                attachment_type = attachment.get("media_type")
                media = attachment.get("media") or {}
                # A video attachment's actual playable file lives at
                # media.source (an mp4 URL) -- full_picture is only ever a
                # static preview frame, never usable for playback.
                if attachment_type == "video" and media.get("source"):
                    media_type = "VIDEO"
                    media_url = media["source"]
                    thumbnail_url = (media.get("image") or {}).get("src") or p.get("full_picture")
                elif attachment_type == "album":
                    media_type = "CAROUSEL_ALBUM"
                    media_url = p.get("full_picture")
                    thumbnail_url = p.get("full_picture")
                else:
                    media_type = "IMAGE"
                    media_url = p.get("full_picture")
                    thumbnail_url = p.get("full_picture")
                media_url, thumbnail_url = _rehost_pair(
                    sb,
                    existing_fb.get(p["id"]),
                    media_url,
                    thumbnail_url,
                    f"synced-posts/{concept_id}/facebook/{p['id']}",
                )
                fb_post_rows.append({
                    "concept_id": concept_id,
                    "platform": "facebook",
                    "post_id": p["id"],
                    "media_type": media_type,
                    "caption": p.get("message"),
                    "permalink": p.get("permalink_url"),
                    "media_url": media_url,
                    "thumbnail_url": thumbnail_url,
                    "posted_at": p.get("created_time"),
                    "like_count": int(((p.get("likes") or {}).get("summary") or {}).get("total_count") or 0),
                    "comments_count": int(((p.get("comments") or {}).get("summary") or {}).get("total_count") or 0),
                    "insights": {"shares": int((p.get("shares") or {}).get("count") or 0)},
                    "synced_at": end_dt.isoformat(),
                })
            if fb_post_rows:
                sb.table("social_posts").upsert(
                    fb_post_rows,
                    on_conflict="concept_id,platform,post_id",
                ).execute()
                print(f"[meta_organic] upserted {len(fb_post_rows)} FB posts into social_posts", flush=True)
        except Exception as e:
            print(f"[meta_organic] FB posts fetch failed: {e}", flush=True)

        # Step 4d: Page insights — fetch each metric individually so one bad name
        # doesn't kill the whole call (Meta API v21.0 is strict on metric names).
        METRIC_CANDIDATES = [
            ("page_impressions",        "day"),
            ("page_impressions_unique", "day"),
            ("page_fan_adds",           "day"),
            ("page_post_engagements",   "day"),
            ("page_engaged_users",      "day"),
        ]
        page_metrics: dict[str, Any] = {}
        failed_metrics: list[str] = []
        for metric_name, period in METRIC_CANDIDATES:
            try:
                resp = _get(f"{GRAPH_BASE}/{page_id}/insights", {
                    "access_token": page_token,
                    "metric": metric_name,
                    "period": period,
                    "since": start_dt.isoformat(),
                    "until": end_dt.isoformat(),
                })
                for item in resp.get("data", []):
                    vals = item.get("values") or []
                    page_metrics[item["name"]] = sum(v.get("value", 0) for v in vals)
            except Exception as e:
                failed_metrics.append(f"{metric_name}: {str(e)[:60]}")
        fb_page["metrics"] = page_metrics
        if failed_metrics:
            print(f"[meta_organic] FB metric failures: {failed_metrics}", flush=True)
        if page_metrics:
            print(f"[meta_organic] FB metrics fetched: {list(page_metrics.keys())}", flush=True)
        else:
            fb_page["insights_error"] = "No metrics available — Page token may lack read_insights permission"

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

    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id": concept_id,
            "platform": "meta_organic",
            "snapshot_date": end_dt.isoformat(),
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

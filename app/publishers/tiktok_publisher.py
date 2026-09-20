"""
tiktok_publisher.py
----------------------
Post to TikTok via the Content Posting API (v2) — Direct Post, video only
(TikTok has no photo-post equivalent to Instagram's feed image).

Credentials needed (stored in platform_credentials table, platform='tiktok',
scoped per concept/brand like Meta's Instagram/Facebook):
  access_token  — short-lived (24h) OAuth token; publishing_routes.py
                  refreshes it just-in-time before calling any function here
  open_id       — TikTok's per-user account identifier

Uses PULL_FROM_URL (TikTok fetches the video itself from our public
Supabase Storage URL) rather than chunked file upload — simpler, and our
video assets already live at a public URL by the time a post is
scheduled.

privacy_level defaults to SELF_ONLY: TikTok requires the app to pass
their content-review/audit process before an unaudited app is allowed
to publish PUBLIC_TO_EVERYONE — attempting that before approval fails
with an authorization_error. Once the app is approved, pass
privacy_level="PUBLIC_TO_EVERYONE" explicitly.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

API_BASE = "https://open.tiktokapis.com/v2"
_STATUS_POLL_INTERVAL = 3    # seconds between status checks
_STATUS_POLL_TIMEOUT = 120   # seconds before giving up (video download+processing can be slow)


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _handle(resp: httpx.Response, context: str) -> dict:
    try:
        data = resp.json()
    except Exception:
        data = {}
    error = data.get("error") or {}
    if not resp.is_success or (error.get("code") and error.get("code") != "ok"):
        msg = error.get("message") or resp.text[:300]
        raise RuntimeError(f"TikTok {context}: {resp.status_code} — {msg}")
    return data.get("data") or {}


def _wait_for_publish(publish_id: str, access_token: str, client: httpx.Client) -> dict:
    """Polls publish status until PUBLISH_COMPLETE or FAILED.
    Statuses: PROCESSING_DOWNLOAD -> PROCESSING_UPLOAD -> PUBLISH_COMPLETE | FAILED."""
    deadline = time.time() + _STATUS_POLL_TIMEOUT
    while time.time() < deadline:
        r = client.post(
            f"{API_BASE}/post/publish/status/fetch/",
            json={"publish_id": publish_id},
            headers=_headers(access_token),
        )
        data = _handle(r, "publish status")
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            return data
        if status == "FAILED":
            reason = data.get("fail_reason") or "unknown reason"
            raise RuntimeError(f"TikTok video publish failed: {reason}")
        time.sleep(_STATUS_POLL_INTERVAL)
    raise RuntimeError(f"TikTok publish did not complete within {_STATUS_POLL_TIMEOUT}s (still processing)")


def _query_creator_info(access_token: str, client: httpx.Client) -> dict:
    r = client.post(f"{API_BASE}/post/publish/creator_info/query/", json={}, headers=_headers(access_token))
    return _handle(r, "creator info query")


def post_tiktok_video(
    *,
    open_id: str,  # noqa: ARG001 -- not required by the API itself (token scopes the account), kept for symmetry/logging with other publishers
    access_token: str,
    video_url: str,
    caption: str,
    privacy_level: str = "SELF_ONLY",
    disable_duet: bool = False,
    disable_comment: bool = False,
    disable_stitch: bool = False,
) -> dict:
    """
    Direct Post, video, PULL_FROM_URL source:
      1. Query creator_info — TikTok requires this call before every publish
         (it's a documented compliance step, not just a UI nicety) and it's
         also the only way to know which privacy_level values this specific
         creator account actually allows; sending one outside that list (or
         skipping the query entirely) gets rejected citing the content
         sharing guidelines.
      2. Init publish (TikTok starts pulling the video from video_url)
      3. Poll status until PUBLISH_COMPLETE
    caption maps to TikTok's "title" field (shown as the post description).
    """
    with httpx.Client(timeout=45.0) as c:
        creator = _query_creator_info(access_token, c)
        allowed_privacy = creator.get("privacy_level_options") or []
        if privacy_level not in allowed_privacy:
            privacy_level = "SELF_ONLY" if "SELF_ONLY" in allowed_privacy else (allowed_privacy[0] if allowed_privacy else privacy_level)

        body = {
            "post_info": {
                "title": caption[:2200],  # TikTok's caption limit
                "privacy_level": privacy_level,
                "disable_duet": disable_duet or bool(creator.get("duet_disabled")),
                "disable_comment": disable_comment or bool(creator.get("comment_disabled")),
                "disable_stitch": disable_stitch or bool(creator.get("stitch_disabled")),
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": video_url,
            },
        }
        r1 = c.post(f"{API_BASE}/post/publish/video/init/", json=body, headers=_headers(access_token))
        init_data = _handle(r1, "init video publish")
        publish_id = init_data["publish_id"]

        result = _wait_for_publish(publish_id, access_token, c)

    post_ids = result.get("publicly_available_post_id") or []
    post_id = post_ids[0] if post_ids else publish_id
    return {
        "platform": "tiktok",
        "post_id": post_id,
        # TikTok doesn't return a direct share URL from this API; the
        # canonical share-link format needs the account's username, which
        # this token doesn't expose — left None, filled in manually if needed.
        "post_url": None,
        "publish_id": publish_id,
    }


def get_tiktok_post_insights(*, video_id: str, access_token: str) -> dict:
    """Fetch view/like/comment/share counts for one of this account's own
    videos. TikTok's query endpoint requires filtering by video id."""
    fields = "id,view_count,like_count,comment_count,share_count"
    with httpx.Client(timeout=15.0) as c:
        r = c.post(
            f"{API_BASE}/video/query/",
            params={"fields": fields},
            json={"filters": {"video_ids": [video_id]}},
            headers=_headers(access_token),
        )
    data = _handle(r, "video insights")
    videos = data.get("videos") or []
    if not videos:
        return {"error": "video not found"}
    v = videos[0]
    return {
        "view_count": v.get("view_count", 0),
        "like_count": v.get("like_count", 0),
        "comment_count": v.get("comment_count", 0),
        "share_count": v.get("share_count", 0),
    }


def test_credentials(*, access_token: str) -> dict:
    """Verify the token is valid and fetch the connected account's display name."""
    fields = "open_id,display_name,follower_count"
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{API_BASE}/user/info/", params={"fields": fields}, headers=_headers(access_token))
    data = _handle(r, "credential test")
    user = data.get("user") or {}
    return {"ok": True, "account_name": user.get("display_name"), "open_id": user.get("open_id")}

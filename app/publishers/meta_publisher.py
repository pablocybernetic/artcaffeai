"""
meta_publisher.py
-----------------
Post to Instagram Business Account and/or Facebook Page via Meta Graph API v21.0.

Credentials needed (stored in platform_credentials table):
  access_token  — long-lived Page Access Token from Meta Business Manager
  page_id       — Facebook Page ID
  ig_user_id    — Instagram Business Account ID (linked to the page)
"""
from __future__ import annotations

import time
from typing import Optional
import httpx

GRAPH = "https://graph.facebook.com/v21.0"
_CONTAINER_POLL_INTERVAL = 3   # seconds between status checks
_CONTAINER_POLL_TIMEOUT  = 90  # seconds before giving up


def _handle(resp: httpx.Response, context: str) -> dict:
    if not resp.is_success:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        raise RuntimeError(f"Meta {context}: {resp.status_code} — {msg}")
    return resp.json()


def _page_token(page_id: str, access_token: str, client: httpx.Client) -> str:
    """Exchange a System User token for a Page Access Token."""
    try:
        r = client.get(
            f"{GRAPH}/{page_id}",
            params={"fields": "access_token", "access_token": access_token},
            timeout=10.0,
        )
        if r.is_success:
            data = r.json()
            if "access_token" in data:
                return data["access_token"]
    except Exception:
        pass
    return access_token


def _wait_for_container(container_id: str, access_token: str, client: httpx.Client) -> None:
    """
    Poll the media container until Instagram finishes processing it.
    Status codes: IN_PROGRESS → FINISHED (publish OK) or ERROR / EXPIRED (fail).
    Raises RuntimeError if processing fails or times out.
    """
    deadline = time.time() + _CONTAINER_POLL_TIMEOUT
    while time.time() < deadline:
        r = client.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=10.0,
        )
        data = _handle(r, "container status")
        status = data.get("status_code") or data.get("status", "")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram media container failed with status: {status}")
        time.sleep(_CONTAINER_POLL_INTERVAL)
    raise RuntimeError(
        f"Instagram media container did not finish processing within {_CONTAINER_POLL_TIMEOUT}s"
    )


def post_instagram(
    *,
    ig_user_id: str,
    access_token: str,
    image_url: str,
    caption: str,
) -> dict:
    """
    Two-step IG feed photo publish:
      1. Create media container
      2. Poll until FINISHED
      3. Publish container
    """
    with httpx.Client(timeout=45.0) as c:
        # Step 1 — create media container
        r1 = c.post(
            f"{GRAPH}/{ig_user_id}/media",
            params={"image_url": image_url, "caption": caption, "access_token": access_token},
        )
        creation_id = _handle(r1, "create container")["id"]

        # Step 2 — wait for Instagram to finish processing the image
        _wait_for_container(creation_id, access_token, c)

        # Step 3 — publish
        r2 = c.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": creation_id, "access_token": access_token},
        )
        post_id = _handle(r2, "publish container")["id"]

    return {
        "platform": "instagram",
        "post_id": post_id,
        "post_url": f"https://www.instagram.com/p/{post_id}/",
    }


def post_instagram_reel(
    *,
    ig_user_id: str,
    access_token: str,
    video_url: str,
    caption: str,
    cover_url: Optional[str] = None,
) -> dict:
    """
    Publish a Reel (short video) to Instagram.
    video_url must be a publicly accessible MP4/MOV URL.
    """
    with httpx.Client(timeout=120.0) as c:
        params: dict = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": access_token,
            "share_to_feed": "true",
        }
        if cover_url:
            params["cover_url"] = cover_url

        r1 = c.post(f"{GRAPH}/{ig_user_id}/media", params=params)
        creation_id = _handle(r1, "create reel container")["id"]

        # Reels take longer to process
        _wait_for_container(creation_id, access_token, c)

        r2 = c.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": creation_id, "access_token": access_token},
        )
        post_id = _handle(r2, "publish reel")["id"]

    return {
        "platform": "instagram",
        "post_id": post_id,
        "post_url": f"https://www.instagram.com/reel/{post_id}/",
        "media_type": "reel",
    }


def get_instagram_post_insights(
    *,
    post_id: str,
    access_token: str,
) -> dict:
    """
    Fetch reach, impressions, likes, comments, shares for a published post.
    Returns a dict of metric → value.
    """
    METRICS = "reach,impressions,likes,comments,shares,saved,total_interactions"
    with httpx.Client(timeout=15.0) as c:
        r = c.get(
            f"{GRAPH}/{post_id}/insights",
            params={"metric": METRICS, "access_token": access_token},
        )
    if not r.is_success:
        return {"error": r.text[:200]}
    data = r.json().get("data", [])
    return {item["name"]: item.get("values", [{}])[0].get("value", item.get("value", 0))
            for item in data}


def post_facebook(
    *,
    page_id: str,
    access_token: str,
    message: str,
    image_url: Optional[str] = None,
) -> dict:
    """Post a photo or text update to a Facebook Page."""
    with httpx.Client(timeout=45.0) as c:
        page_access_token = _page_token(page_id, access_token, c)

        if image_url:
            r = c.post(
                f"{GRAPH}/{page_id}/photos",
                params={"url": image_url, "caption": message, "access_token": page_access_token},
            )
            data = _handle(r, "photo post")
            post_id = data.get("post_id") or data.get("id", "")
        else:
            r = c.post(
                f"{GRAPH}/{page_id}/feed",
                params={"message": message, "access_token": page_access_token},
            )
            post_id = _handle(r, "feed post")["id"]

    return {
        "platform": "facebook",
        "post_id": post_id,
        "post_url": f"https://www.facebook.com/{post_id}",
    }


def post_instagram_story(
    *,
    ig_user_id: str,
    access_token: str,
    image_url: str,
) -> dict:
    """
    Publish an image as an ephemeral Instagram Story (disappears after 24h).
    Stories do not support captions via the Graph API.
    """
    with httpx.Client(timeout=45.0) as c:
        r1 = c.post(
            f"{GRAPH}/{ig_user_id}/media",
            params={
                "image_url": image_url,
                "media_type": "STORIES",
                "access_token": access_token,
            },
        )
        creation_id = _handle(r1, "create story container")["id"]
        _wait_for_container(creation_id, access_token, c)

        r2 = c.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": creation_id, "access_token": access_token},
        )
        post_id = _handle(r2, "publish story")["id"]

    return {
        "platform": "instagram",
        "post_id": post_id,
        "post_url": f"https://www.instagram.com/stories/{ig_user_id}/{post_id}/",
        "media_type": "story",
    }


def post_facebook_story(
    *,
    page_id: str,
    access_token: str,
    image_url: str,
) -> dict:
    """
    Publish a photo as an ephemeral Facebook Story (disappears after 24h).
    Uses the /photo_stories endpoint.
    """
    with httpx.Client(timeout=45.0) as c:
        page_token = _page_token(page_id, access_token, c)
        r = c.post(
            f"{GRAPH}/{page_id}/photo_stories",
            params={
                "url": image_url,
                "access_token": page_token,
            },
        )
        data = _handle(r, "facebook photo story")

    story_id = data.get("id", "")
    return {
        "platform": "facebook",
        "post_id": story_id,
        "post_url": f"https://www.facebook.com/stories/{story_id}",
        "media_type": "story",
    }


def post_facebook_reel(
    *,
    page_id: str,
    access_token: str,
    video_url: str,
    caption: str,
) -> dict:
    """
    Publish a video as a Facebook Reel.
    video_url must be a publicly accessible MP4 URL.
    Three-step process: init upload → upload bytes → finish & publish.
    """
    with httpx.Client(timeout=120.0) as c:
        page_token = _page_token(page_id, access_token, c)

        # Step 1 — initialise the reel upload session
        r1 = c.post(
            f"{GRAPH}/{page_id}/video_reels",
            params={"upload_phase": "start", "access_token": page_token},
        )
        init_data = _handle(r1, "init reel upload")
        video_id = init_data["video_id"]
        upload_url = init_data["upload_url"]

        # Step 2 — download video and PUT to the upload URL
        vid = c.get(video_url, follow_redirects=True, timeout=60.0)
        if not vid.is_success:
            raise RuntimeError(f"Could not fetch video for reel upload: {vid.status_code}")
        upload_r = c.put(
            upload_url,
            content=vid.content,
            headers={
                "Authorization": f"OAuth {page_token}",
                "offset": "0",
                "file_size": str(len(vid.content)),
            },
            timeout=120.0,
        )
        if not upload_r.is_success:
            raise RuntimeError(f"Reel video upload failed: {upload_r.status_code} — {upload_r.text[:200]}")

        # Step 3 — finish and publish
        r3 = c.post(
            f"{GRAPH}/{page_id}/video_reels",
            params={
                "video_id": video_id,
                "upload_phase": "finish",
                "video_state": "PUBLISHED",
                "description": caption,
                "access_token": page_token,
            },
        )
        _handle(r3, "finish reel upload")

    return {
        "platform": "facebook",
        "post_id": video_id,
        "post_url": f"https://www.facebook.com/reel/{video_id}",
        "media_type": "reel",
    }


def test_credentials(*, access_token: str, page_id: str) -> dict:
    """Verify token and page access."""
    with httpx.Client(timeout=15.0) as c:
        r_me = c.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": access_token})
        _handle(r_me, "credential test")

        account_name: str = page_id
        try:
            page_token = _page_token(page_id, access_token, c)
            r_page = c.get(
                f"{GRAPH}/{page_id}",
                params={"fields": "id,name", "access_token": page_token},
            )
            if r_page.is_success:
                account_name = r_page.json().get("name", page_id)
        except Exception:
            try:
                r_pages = c.get(
                    f"{GRAPH}/me/accounts",
                    params={"fields": "id,name", "access_token": access_token},
                )
                if r_pages.is_success:
                    pages = r_pages.json().get("data", [])
                    matched = next((p["name"] for p in pages if str(p.get("id")) == str(page_id)), None)
                    account_name = matched or (pages[0]["name"] if pages else page_id)
            except Exception:
                pass

    return {"ok": True, "account_name": account_name}

"""
twitter_publisher.py
--------------------
Post to Twitter / X using Tweepy (OAuth 1.0a v2 client).

Credentials stored in platform_credentials (platform='twitter'):
  access_token              — OAuth 1.0a access token
  extra_json.access_token_secret — OAuth 1.0a access token secret
  developer_token           — API Key (consumer key)
  extra_json.api_key_secret — API Key secret (consumer secret)
  account_name              — @handle (display only)
"""
from __future__ import annotations

from typing import Optional


def _build_client(api_key: str, api_key_secret: str, access_token: str, access_token_secret: str):
    import tweepy  # noqa: PLC0415
    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_key_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )


def _upload_media(api_key: str, api_key_secret: str, access_token: str, access_token_secret: str, image_url: str) -> Optional[int]:
    """Download image_url and upload to Twitter media API. Returns media_id or None."""
    try:
        import tweepy  # noqa: PLC0415
        import httpx  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        import os  # noqa: PLC0415

        auth = tweepy.OAuth1UserHandler(api_key, api_key_secret, access_token, access_token_secret)
        api_v1 = tweepy.API(auth)

        r = httpx.get(image_url, timeout=20, follow_redirects=True)
        r.raise_for_status()

        suffix = ".jpg"
        ct = r.headers.get("content-type", "")
        if "png" in ct:
            suffix = ".png"
        elif "gif" in ct:
            suffix = ".gif"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(r.content)
            tmp_path = f.name
        try:
            media = api_v1.media_upload(filename=tmp_path)
            return media.media_id
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        print(f"[twitter_publisher] media upload failed: {e}", flush=True)
        return None


def post_tweet(
    api_key: str,
    api_key_secret: str,
    access_token: str,
    access_token_secret: str,
    text: str,
    image_url: Optional[str] = None,
) -> dict:
    client = _build_client(api_key, api_key_secret, access_token, access_token_secret)

    media_ids = None
    if image_url:
        mid = _upload_media(api_key, api_key_secret, access_token, access_token_secret, image_url)
        if mid:
            media_ids = [mid]

    resp = client.create_tweet(text=text[:280], media_ids=media_ids)
    tweet_id = str(resp.data["id"]) if resp.data else None
    return {
        "post_id": tweet_id,
        "post_url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None,
    }


def test_credentials(
    api_key: str,
    api_key_secret: str,
    access_token: str,
    access_token_secret: str,
) -> dict:
    client = _build_client(api_key, api_key_secret, access_token, access_token_secret)
    me = client.get_me()
    handle = f"@{me.data.username}" if me and me.data else "Twitter account"
    return {"ok": True, "account_name": handle}

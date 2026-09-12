"""
tiktok_auth.py
----------------
OAuth 2.0 helpers for TikTok's Content Posting API + Display API (v2).

Unlike Meta (a long-lived Page Access Token an admin can generate once in
Business Manager and paste into Settings), TikTok requires an actual
user-consent OAuth flow per connected account, and the resulting access
token expires in 24h (refresh_token is valid ~365 days and must be used
to mint a new one) — see tiktok_routes.py for the authorize/callback
routes and publishing_routes.py / tiktok_organic_connector.py for where
tokens get refreshed just-in-time before use.

App-level Client Key/Secret (one pair, shared across all three brands)
are stored encrypted in app_settings under TIKTOK_APP_SETTINGS_KEY — see
tiktok_routes.py. Per-brand access_token/refresh_token/open_id live in
platform_credentials (platform='tiktok', concept_id=<brand>), mirroring
how Meta's Instagram/Facebook credentials are scoped per brand.

Docs: https://developers.tiktok.com/doc/oauth-user-access-token-management
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# video.publish: Content Posting API (Direct Post)
# video.upload: required alongside video.publish for the init/publish flow
# user.info.basic: display name/avatar
# user.info.stats: follower_count/likes_count/video_count (organic analytics)
# video.list: list this user's own videos + per-video stats
DEFAULT_SCOPES = "user.info.basic,user.info.stats,video.list,video.publish,video.upload"


def build_authorize_url(*, client_key: str, redirect_uri: str, state: str, scopes: str = DEFAULT_SCOPES) -> str:
    """`state` should encode enough to identify which brand (concept_id)
    is connecting — TikTok echoes it back verbatim to the redirect_uri."""
    params = {
        "client_key": client_key,
        "scope": scopes,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _handle(resp: httpx.Response, context: str) -> dict:
    data = {}
    try:
        data = resp.json()
    except Exception:
        pass
    if not resp.is_success or data.get("error"):
        # TikTok's token endpoint returns 200 with an "error" field on
        # failure, not just a non-2xx status — check both.
        err = data.get("error_description") or data.get("error") or resp.text[:300]
        raise RuntimeError(f"TikTok {context}: {resp.status_code} — {err}")
    return data


def exchange_code_for_token(*, client_key: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Returns {access_token, expires_in, refresh_token, refresh_expires_in, open_id, scope, token_type}."""
    with httpx.Client(timeout=20.0) as c:
        r = c.post(
            TOKEN_URL,
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    return _handle(r, "token exchange")


def refresh_access_token(*, client_key: str, client_secret: str, refresh_token: str) -> dict:
    """Same response shape as exchange_code_for_token — a fresh
    access_token/refresh_token pair (TikTok rotates the refresh_token too)."""
    with httpx.Client(timeout=20.0) as c:
        r = c.post(
            TOKEN_URL,
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    return _handle(r, "token refresh")


def is_token_expired(expires_at: Optional[str], *, skew_seconds: int = 300) -> bool:
    """`expires_at` is an ISO timestamp computed at token-mint time
    (now + expires_in). Treated as expired a few minutes early so a
    request never starts mid-flight with a token that dies mid-call."""
    if not expires_at:
        return True
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    from datetime import timedelta
    return datetime.now(timezone.utc) >= (dt - timedelta(seconds=skew_seconds))


def ensure_fresh_token(sb, concept_id: str, creds: dict) -> str:
    """Shared by tiktok_organic_connector, publishing_routes' tiktok
    publish branch, and the /publish/test/tiktok endpoint — returns a
    valid access_token, transparently refreshing and persisting a new
    one first if the stored one is expired/near expiry."""
    from datetime import datetime, timedelta, timezone as _tz
    from app_settings import get_setting
    from secrets_crypto import decrypt_value

    extra = creds.get("extra_json") or {}
    if not is_token_expired(extra.get("token_expires_at")):
        return creds["access_token"]

    app_creds = get_setting(sb, "tiktok_app_credentials", {}) or {}
    client_key = app_creds.get("client_key")
    client_secret_enc = app_creds.get("client_secret_enc")
    if not client_key or not client_secret_enc:
        raise RuntimeError("TikTok app Client Key/Secret not configured — set them in Settings → Integrations")
    client_secret = decrypt_value(client_secret_enc)

    refreshed = refresh_access_token(client_key=client_key, client_secret=client_secret, refresh_token=extra.get("refresh_token", ""))
    new_expires_at = (datetime.now(_tz.utc) + timedelta(seconds=refreshed.get("expires_in", 86400))).isoformat()

    sb.table("platform_credentials").update({
        "access_token": refreshed["access_token"],
        "extra_json": {
            **extra,
            "refresh_token": refreshed.get("refresh_token", extra.get("refresh_token")),
            "token_expires_at": new_expires_at,
        },
        "updated_at": datetime.now(_tz.utc).isoformat(),
    }).eq("platform", "tiktok").eq("concept_id", concept_id).execute()

    print(f"[tiktok_auth] refreshed access token for concept={concept_id[:8]}", flush=True)
    return refreshed["access_token"]

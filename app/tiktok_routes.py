"""
tiktok_routes.py
------------------
TikTok-specific routes not covered by publishing_routes.py's generic
/publish/credentials endpoint: the app-level Client Key/Secret (one pair,
shared by all three brands — obtained once from the TikTok Developer
Portal) and the OAuth authorize/callback flow needed because TikTok, unlike
Meta, requires actual per-account user consent rather than a pasted
long-lived token.

Prefix: /tiktok
Auth:   X-Api-Key header (same pattern as publishing_routes.py)

Endpoints:
  GET   /tiktok/settings                — app credential status (masked) + per-concept connection status
  POST  /tiktok/settings                — save Client Key/Secret (encrypted)
  GET   /tiktok/oauth/authorize-url     — build the TikTok consent-screen URL for one concept
  POST  /tiktok/oauth/callback          — exchange the code TikTok returned for tokens, store them
  POST  /tiktok/oauth/disconnect        — deactivate a concept's TikTok credentials

The actual redirect_uri TikTok sends the user back to is a *frontend*
route (frontend/src/routes/integrations.tiktok-callback.tsx) — TikTok
requires the redirect_uri to be a fixed, pre-registered HTTPS URL, and
this backend has no HTTPS domain of its own (see locations module's
Shopify integration for the identical constraint). The frontend route
reads TikTok's `code`/`state` query params and POSTs them here
server-to-server (plain HTTP is fine — no browser mixed-content rule
applies to a server-to-server call).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

from connectors.tiktok_auth import build_authorize_url, exchange_code_for_token
from secrets_crypto import encrypt_value, decrypt_value, mask_value

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
# Must exactly match a redirect URI registered on the TikTok app —
# TikTok rejects any mismatch, even a trailing-slash difference.
TIKTOK_REDIRECT_URI = os.environ.get(
    "TIKTOK_REDIRECT_URI", "https://marketing.artcaffe.co.ke/integrations/tiktok-callback"
)

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

SETTINGS_KEY = "tiktok_app_credentials"


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/tiktok", dependencies=[Depends(require_api_key)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app_credentials() -> tuple[Optional[str], Optional[str]]:
    """Returns (client_key, client_secret) or (None, None) if not configured."""
    from app_settings import get_setting  # noqa: PLC0415
    saved = get_setting(sb, SETTINGS_KEY, {}) or {}
    client_key = saved.get("client_key")
    enc = saved.get("client_secret_enc")
    if not client_key or not enc:
        return None, None
    try:
        return client_key, decrypt_value(enc)
    except ValueError:
        return None, None


class AppSettingsUpdate(BaseModel):
    client_key: Optional[str] = None
    client_secret: Optional[str] = None


@router.get("/settings")
def get_settings():
    from app_settings import get_setting  # noqa: PLC0415
    saved = get_setting(sb, SETTINGS_KEY, {}) or {}

    concepts_res = sb.table("concepts").select("id,name").execute()
    creds_res = sb.table("platform_credentials").select("concept_id,account_name,is_active").eq("platform", "tiktok").execute()
    by_concept = {r["concept_id"]: r for r in (creds_res.data or []) if r.get("concept_id")}

    connections = [
        {
            "concept_id": c["id"],
            "concept_name": c.get("name"),
            "connected": bool(by_concept.get(c["id"], {}).get("is_active")),
            "account_name": by_concept.get(c["id"], {}).get("account_name"),
        }
        for c in (concepts_res.data or [])
    ]

    return {
        "ok": True,
        "data": {
            "client_key": saved.get("client_key"),
            "client_secret_masked": mask_value(_app_credentials()[1]),
            "client_secret_configured": bool(saved.get("client_secret_enc")),
            "redirect_uri": TIKTOK_REDIRECT_URI,
            "connections": connections,
        },
    }


@router.post("/settings")
def update_settings(body: AppSettingsUpdate):
    from app_settings import get_setting, set_setting  # noqa: PLC0415
    saved = get_setting(sb, SETTINGS_KEY, {}) or {}
    if body.client_key:
        saved["client_key"] = body.client_key
    if body.client_secret:
        saved["client_secret_enc"] = encrypt_value(body.client_secret)
    set_setting(sb, SETTINGS_KEY, saved)
    return get_settings()


@router.get("/oauth/authorize-url")
def get_authorize_url(concept_id: str):
    client_key, _ = _app_credentials()
    if not client_key:
        raise HTTPException(400, "TikTok Client Key/Secret not configured yet — set them in Settings → Integrations first")

    # concept_id round-trips through TikTok verbatim as `state` — this is
    # how the callback knows which brand just connected. Good enough for
    # an internal admin tool; a public-facing consumer would want a
    # signed/random CSRF token here instead of the raw id.
    url = build_authorize_url(client_key=client_key, redirect_uri=TIKTOK_REDIRECT_URI, state=concept_id)
    return {"ok": True, "authorize_url": url}


class OAuthCallbackRequest(BaseModel):
    code: str
    concept_id: str  # the `state` TikTok echoed back


@router.post("/oauth/callback")
def oauth_callback(body: OAuthCallbackRequest):
    client_key, client_secret = _app_credentials()
    if not client_key or not client_secret:
        raise HTTPException(400, "TikTok Client Key/Secret not configured")

    try:
        token_data = exchange_code_for_token(
            client_key=client_key, client_secret=client_secret,
            code=body.code, redirect_uri=TIKTOK_REDIRECT_URI,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token_data.get("expires_in", 86400))).isoformat()

    row = {
        "platform": "tiktok",
        "access_token": token_data["access_token"],
        "is_active": True,
        "updated_at": _now(),
        "extra_json": {
            "open_id": token_data.get("open_id"),
            "refresh_token": token_data.get("refresh_token"),
            "token_expires_at": expires_at,
            "scope": token_data.get("scope"),
        },
    }

    from publishing_routes import _upsert_platform_credentials  # noqa: PLC0415
    _upsert_platform_credentials(sb, row, body.concept_id)

    # Best-effort: fetch display name right away so Settings shows
    # something other than "Connected" with no account name.
    try:
        from publishers.tiktok_publisher import test_credentials  # noqa: PLC0415
        info = test_credentials(access_token=token_data["access_token"])
        if info.get("account_name"):
            q = sb.table("platform_credentials").update({"account_name": info["account_name"]}).eq("platform", "tiktok")
            q.eq("concept_id", body.concept_id).execute()
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "concept_id": body.concept_id}


@router.post("/oauth/disconnect")
def disconnect(concept_id: str):
    sb.table("platform_credentials").update({"is_active": False, "updated_at": _now()}).eq("platform", "tiktok").eq("concept_id", concept_id).execute()
    return {"ok": True}

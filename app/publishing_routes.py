"""
publishing_routes.py
--------------------
FastAPI router for social media publishing.

Prefix: /publish
Auth:   X-Api-Key header (same pattern as agent_routes.py)

Endpoints:
  POST  /publish/post                  — publish a content item to selected platforms
  GET   /publish/credentials           — get credential status (tokens masked)
  POST  /publish/credentials           — save / update platform credentials
  POST  /publish/test/{platform}       — test credentials for a platform
  GET   /publish/history/{item_id}     — published post history for a content item

Open-source publishing skills (see publishing_agent.py):
  - DuckDuckGo hashtag research: finds trending Nairobi/Kenya food hashtags
  - Claude Haiku caption optimisation: adapts caption per platform style/limits
  These run before every publish and fall back gracefully if unavailable.

Supported platforms:
  instagram, facebook, linkedin, google_ads, twitter, whatsapp
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

from publishers.meta_publisher import (
    post_instagram,
    post_facebook,
    test_credentials as test_meta,
)
from publishers.linkedin_publisher import (
    post_linkedin,
    test_credentials as test_linkedin,
)
from publishers.google_ads_publisher import (
    create_responsive_search_ad,
    test_credentials as test_google_ads,
)
from publishers.twitter_publisher import (
    post_tweet,
    test_credentials as test_twitter,
)
from publishers.whatsapp_publisher import (
    post_whatsapp,
    test_credentials as test_whatsapp,
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/publish", dependencies=[Depends(require_api_key)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(token: Optional[str]) -> Optional[str]:
    if not token or len(token) < 8:
        return None
    return "●" * (len(token) - 6) + token[-6:]


def _get_creds(platform: str, sb_client: Optional[Client] = None) -> Optional[dict]:
    try:
        client = sb_client or sb
        res = client.table("platform_credentials").select("*").eq("platform", platform).eq("is_active", True).maybe_single().execute()
        return res.data if res is not None else None
    except Exception:
        return None


def _save_publish_result(
    *,
    sb_client: Client,
    content_item_id: str,
    concept_id: Optional[str],
    platform: str,
    result: dict,
    error: Optional[str] = None,
) -> None:
    sb_client.table("published_posts").insert({
        "content_item_id": content_item_id,
        "concept_id": concept_id,
        "platform": platform,
        "platform_post_id": result.get("post_id") or result.get("resource_name"),
        "platform_post_url": result.get("post_url"),
        "status": "failed" if error else "published",
        "error_message": error,
        "published_at": _now(),
        "created_at": _now(),
    }).execute()


def _execute_publish(
    sb_client: Client,
    content_item_id: str,
    platforms: list[str],
    anthropic: Optional[Any] = None,
) -> dict:
    """
    Core publish logic shared by the REST endpoint and the job_runner.

    When anthropic client is provided, runs pre-publish skills (publishing_agent.py):
      1. DuckDuckGo hashtag research
      2. Claude Haiku caption optimisation per platform

    Returns {"ok": bool, "results": {platform: {...}}}
    """
    # Fetch content item
    item_res = (
        sb_client.table("content_items")
        .select("id,brief_id,headline,caption,body,platform,asset_ids,status,type,metadata")
        .eq("id", content_item_id)
        .single()
        .execute()
    )
    if item_res is None or not item_res.data:
        raise RuntimeError(f"Content item not found: {content_item_id}")
    item: dict[str, Any] = item_res.data

    # Fetch concept_id from brief
    brief_res = sb_client.table("content_briefs").select("concept_id").eq("id", item["brief_id"]).single().execute()
    concept_id: Optional[str] = (brief_res.data or {}).get("concept_id") if brief_res is not None else None

    # Resolve first asset image URL
    image_url: Optional[str] = None
    if item.get("asset_ids"):
        asset_res = (
            sb_client.table("assets")
            .select("public_url")
            .in_("id", item["asset_ids"])
            .eq("asset_type", "image")
            .limit(1)
            .execute()
        )
        if asset_res is not None and asset_res.data:
            image_url = asset_res.data[0].get("public_url")

    headline = item.get("headline") or ""
    raw_caption = item.get("caption") or item.get("body") or ""
    meta = item.get("metadata") or {}

    # --- Publishing Skills: optimise caption per platform ---
    from publishing_agent import optimize_for_platforms  # noqa: PLC0415
    platform_content = optimize_for_platforms(
        anthropic=anthropic,
        headline=headline,
        caption=raw_caption,
        platforms=platforms,
        today=date.today(),
    )

    results: dict[str, Any] = {}

    for platform in platforms:
        try:
            optimised = platform_content.get(platform, {"caption": raw_caption, "hashtags": []})
            opt_caption = optimised["caption"]
            opt_hashtags = optimised["hashtags"]
            hashtag_str = " ".join(opt_hashtags)

            if platform == "instagram":
                creds = _get_creds("meta", sb_client)
                if not creds:
                    raise RuntimeError("Meta credentials not configured")
                if not image_url:
                    raise RuntimeError("Instagram requires an image — no image asset found on this content item")
                text = f"{headline}\n\n{opt_caption}" + (f"\n\n{hashtag_str}" if hashtag_str else "")
                r = post_instagram(
                    ig_user_id=creds["ig_user_id"],
                    access_token=creds["access_token"],
                    image_url=image_url,
                    caption=text,
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            elif platform == "facebook":
                creds = _get_creds("meta", sb_client)
                if not creds:
                    raise RuntimeError("Meta credentials not configured")
                text = f"{headline}\n\n{opt_caption}" + (f"\n\n{hashtag_str}" if hashtag_str else "")
                r = post_facebook(
                    page_id=creds["page_id"],
                    access_token=creds["access_token"],
                    message=text,
                    image_url=image_url,
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            elif platform == "linkedin":
                creds = _get_creds("linkedin", sb_client)
                if not creds:
                    raise RuntimeError("LinkedIn credentials not configured")
                text = f"{headline}\n\n{opt_caption}" + (f"\n\n{hashtag_str}" if hashtag_str else "")
                r = post_linkedin(
                    org_id=creds["org_id"],
                    access_token=creds["access_token"],
                    text=text,
                    image_url=image_url,
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            elif platform == "twitter":
                creds = _get_creds("twitter", sb_client)
                if not creds:
                    raise RuntimeError("Twitter credentials not configured")
                extra = creds.get("extra_json") or {}
                tweet_text = opt_caption + (f" {hashtag_str}" if hashtag_str else "")
                r = post_tweet(
                    api_key=creds["developer_token"],
                    api_key_secret=extra.get("api_key_secret", ""),
                    access_token=creds["access_token"],
                    access_token_secret=extra.get("access_token_secret", ""),
                    text=tweet_text,
                    image_url=image_url,
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            elif platform == "whatsapp":
                creds = _get_creds("whatsapp", sb_client)
                if not creds:
                    raise RuntimeError("WhatsApp credentials not configured")
                # phone_number_id stored in ig_user_id, to_number stored in page_id
                text = f"{headline}\n\n{opt_caption}"
                r = post_whatsapp(
                    phone_number_id=creds["ig_user_id"],
                    access_token=creds["access_token"],
                    to_number=creds["page_id"],
                    text=text,
                    image_url=image_url,
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            elif platform == "google_ads":
                creds = _get_creds("google_ads", sb_client)
                if not creds:
                    raise RuntimeError("Google Ads credentials not configured")
                headlines_list = [headline] if headline else ["Artcaffe — Premium Café"]
                if meta.get("ad_headlines") and isinstance(meta["ad_headlines"], list):
                    headlines_list += [h[:30] for h in meta["ad_headlines"]]
                descriptions_list = [opt_caption[:90]] if opt_caption else ["Visit Artcaffe for the best coffee in Nairobi."]
                r = create_responsive_search_ad(
                    access_token=creds["access_token"],
                    developer_token=creds["developer_token"],
                    customer_id=creds["customer_id"],
                    ad_group_id=creds["ad_group_id"],
                    headlines=headlines_list,
                    descriptions=descriptions_list,
                    final_url=creds.get("final_url") or "https://artcaffe.co.ke",
                )
                _save_publish_result(sb_client=sb_client, content_item_id=content_item_id, concept_id=concept_id, platform=platform, result=r)
                results[platform] = {"ok": True, **r}

            else:
                results[platform] = {"ok": False, "error": f"Unknown platform: {platform}"}

        except Exception as exc:
            error_msg = str(exc)
            _save_publish_result(
                sb_client=sb_client,
                content_item_id=content_item_id,
                concept_id=concept_id,
                platform=platform,
                result={},
                error=error_msg,
            )
            results[platform] = {"ok": False, "error": error_msg}

    # Mark content item and brief as published if all platforms succeeded
    all_ok = all(v.get("ok") for v in results.values())
    if all_ok:
        sb_client.table("content_items").update({"status": "approved", "updated_at": _now()}).eq("id", content_item_id).execute()
        sb_client.table("content_briefs").update({"stage": "published", "updated_at": _now()}).eq("id", item["brief_id"]).execute()

    return {"ok": all_ok, "results": results}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PublishRequest(BaseModel):
    content_item_id: str
    platforms: list[str]          # e.g. ["instagram", "facebook", "linkedin", "google_ads"]


class CredentialsSaveRequest(BaseModel):
    platform: str                 # "meta" | "linkedin" | "google_ads" | "twitter" | "whatsapp"
    access_token: Optional[str] = None
    page_id: Optional[str] = None          # Facebook Page ID  /  WhatsApp: to_number
    ig_user_id: Optional[str] = None       # Instagram User ID / WhatsApp: phone_number_id
    org_id: Optional[str] = None           # LinkedIn Org ID
    developer_token: Optional[str] = None  # Google Ads / Twitter: api_key
    customer_id: Optional[str] = None      # Google Ads
    campaign_id: Optional[str] = None      # Google Ads
    ad_group_id: Optional[str] = None      # Google Ads
    final_url: Optional[str] = None        # Google Ads landing page URL
    account_name: Optional[str] = None     # display name
    # Twitter extra fields stored in extra_json
    api_key_secret: Optional[str] = None          # Twitter API Key Secret
    access_token_secret: Optional[str] = None     # Twitter Access Token Secret


class TestRequest(BaseModel):
    platform: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/post")
def publish_post(req: PublishRequest):
    """
    Publish a content item to the requested social platforms.
    Runs publishing skills (caption optimisation + hashtag research) before posting.
    Returns per-platform results (ok/error).
    """
    try:
        from anthropic import Anthropic  # noqa: PLC0415
        anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY")) if os.environ.get("ANTHROPIC_API_KEY") else None
        return _execute_publish(sb, req.content_item_id, req.platforms, anthropic=anthropic_client)
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@router.get("/credentials")
def get_credentials():
    """Return masked credential status for all platforms (no token values)."""
    rows = sb.table("platform_credentials").select("*").eq("is_active", True).execute()
    status: dict[str, Any] = {}
    for row in (rows.data or []):
        platform = row["platform"]
        status[platform] = {
            "connected": bool(row.get("access_token")),
            "account_name": row.get("account_name"),
            "token_masked": _mask(row.get("access_token")),
            # Meta
            "page_id": row.get("page_id"),
            "ig_user_id": row.get("ig_user_id"),
            # LinkedIn
            "org_id": row.get("org_id"),
            # Google Ads
            "customer_id": row.get("customer_id"),
            "campaign_id": row.get("campaign_id"),
            "ad_group_id": row.get("ad_group_id"),
            "final_url": row.get("final_url"),
        }
    return {"credentials": status}


@router.post("/credentials")
def save_credentials(req: CredentialsSaveRequest):
    """Upsert platform credentials."""
    VALID = {"meta", "linkedin", "google_ads", "twitter", "whatsapp"}
    if req.platform not in VALID:
        raise HTTPException(400, f"platform must be one of {VALID}")

    row: dict[str, Any] = {
        "platform": req.platform,
        "is_active": True,
        "updated_at": _now(),
    }
    if req.access_token:
        row["access_token"] = req.access_token
    if req.account_name:
        row["account_name"] = req.account_name

    if req.platform == "meta":
        if req.page_id:
            row["page_id"] = req.page_id
        if req.ig_user_id:
            row["ig_user_id"] = req.ig_user_id
    elif req.platform == "linkedin":
        if req.org_id:
            row["org_id"] = req.org_id
    elif req.platform == "google_ads":
        if req.developer_token:
            row["developer_token"] = req.developer_token
        if req.customer_id:
            row["customer_id"] = req.customer_id
        if req.campaign_id:
            row["campaign_id"] = req.campaign_id
        if req.ad_group_id:
            row["ad_group_id"] = req.ad_group_id
        if req.final_url:
            row["final_url"] = req.final_url
    elif req.platform == "twitter":
        # API Key (consumer key) stored in developer_token
        if req.developer_token:
            row["developer_token"] = req.developer_token
        # API Key Secret + Access Token Secret stored in extra_json
        extra: dict[str, str] = {}
        if req.api_key_secret:
            extra["api_key_secret"] = req.api_key_secret
        if req.access_token_secret:
            extra["access_token_secret"] = req.access_token_secret
        if extra:
            row["extra_json"] = extra
    elif req.platform == "whatsapp":
        # phone_number_id stored in ig_user_id; to_number stored in page_id
        if req.ig_user_id:
            row["ig_user_id"] = req.ig_user_id
        if req.page_id:
            row["page_id"] = req.page_id

    # Upsert by platform (unique key)
    existing = sb.table("platform_credentials").select("id").eq("platform", req.platform).maybe_single().execute()
    if existing.data:
        sb.table("platform_credentials").update(row).eq("platform", req.platform).execute()
    else:
        row["created_at"] = _now()
        sb.table("platform_credentials").insert(row).execute()

    return {"ok": True, "platform": req.platform}


@router.post("/test/{platform}")
def test_platform(platform: str):
    """Test current credentials for a platform without posting."""
    creds = _get_creds(platform)
    if not creds:
        raise HTTPException(404, f"No credentials saved for platform: {platform}")

    try:
        if platform == "meta":
            result = test_meta(access_token=creds["access_token"], page_id=creds["page_id"])
        elif platform == "linkedin":
            result = test_linkedin(access_token=creds["access_token"], org_id=creds["org_id"])
        elif platform == "google_ads":
            result = test_google_ads(
                access_token=creds["access_token"],
                developer_token=creds["developer_token"],
                customer_id=creds["customer_id"],
            )
        elif platform == "twitter":
            extra = creds.get("extra_json") or {}
            result = test_twitter(
                api_key=creds["developer_token"],
                api_key_secret=extra.get("api_key_secret", ""),
                access_token=creds["access_token"],
                access_token_secret=extra.get("access_token_secret", ""),
            )
        elif platform == "whatsapp":
            result = test_whatsapp(
                phone_number_id=creds["ig_user_id"],
                access_token=creds["access_token"],
            )
        else:
            raise HTTPException(400, f"Unknown platform: {platform}")
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    # Update account_name if test returned one
    if result.get("account_name"):
        sb.table("platform_credentials").update({"account_name": result["account_name"]}).eq("platform", platform).execute()

    return result


@router.get("/history/{content_item_id}")
def publish_history(content_item_id: str):
    """Return all publish attempts for a content item."""
    res = (
        sb.table("published_posts")
        .select("id,platform,platform_post_id,platform_post_url,status,error_message,published_at")
        .eq("content_item_id", content_item_id)
        .order("published_at", desc=True)
        .execute()
    )
    return {"history": res.data or []}

"""
ads_routes.py
-------------
FastAPI router for direct Meta Ads and Google Ads data sync.

Prefix: /data/ads
Auth:   X-Api-Key header

Endpoints:
  POST /data/ads/meta/sync    — pull Meta Ads Insights API → platform_data_snapshots
  POST /data/ads/google/sync  — pull Google Ads reporting API → platform_data_snapshots
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(
    prefix="/data/ads",
    dependencies=[Depends(require_api_key)],
)


def _get_creds(platform: str, concept_id: Optional[str] = None) -> dict:
    """Return the credential row as a flat dict (top-level columns, not credentials_json)."""
    q = sb.table("platform_credentials").select("*").eq("platform", platform).eq("is_active", True)
    q = q.eq("concept_id", concept_id) if concept_id else q.is_("concept_id", "null")
    res = q.limit(1).execute()
    if not res.data:
        return {}
    return res.data[0] or {}


class AdsSyncRequest(BaseModel):
    concept_id: str
    date_range_days: int = 7
    end_date: Optional[str] = None  # YYYY-MM-DD; meta_organic only — window ends here instead of today


# ---------------------------------------------------------------------------
# Meta Ads sync
# ---------------------------------------------------------------------------
@router.post("/meta/sync")
def sync_meta_ads_endpoint(req: AdsSyncRequest):
    """Pull Meta Ads Insights API data and store as a platform_data_snapshot."""
    from connectors.meta_ads_connector import sync_meta_ads  # noqa: PLC0415

    creds = _get_creds("meta", concept_id=req.concept_id)
    access_token = creds.get("access_token")
    ad_account_id = creds.get("ad_account_id")

    if not access_token:
        raise HTTPException(status_code=400, detail="Meta credentials not configured (missing access_token)")
    if not ad_account_id:
        raise HTTPException(
            status_code=400,
            detail="Meta Ad Account ID not configured — add it in Settings → Social publishing → Instagram & Facebook",
        )

    try:
        summary = sync_meta_ads(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            ad_account_id=ad_account_id,
            date_range_days=req.date_range_days,
        )
        return {
            "ok": True,
            "platform": "meta_ads",
            "concept_id": req.concept_id,
            "snapshot": {
                "totals": summary["totals"],
                "campaigns_count": len(summary.get("campaigns", [])),
                "ad_sets_count": len(summary.get("ad_sets", [])),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meta Ads sync failed: {e}")


# ---------------------------------------------------------------------------
# Google Ads sync
# ---------------------------------------------------------------------------
@router.post("/google/sync")
def sync_google_ads_endpoint(req: AdsSyncRequest):
    """Pull Google Ads reporting API data and store as a platform_data_snapshot."""
    from connectors.google_ads_connector import sync_google_ads  # noqa: PLC0415

    creds = _get_creds("google_ads")
    access_token = creds.get("access_token")
    developer_token = creds.get("developer_token")
    customer_id = creds.get("customer_id")

    if not access_token or not developer_token or not customer_id:
        raise HTTPException(
            status_code=400,
            detail="Google Ads credentials incomplete — access_token, developer_token, and customer_id are required",
        )

    try:
        summary = sync_google_ads(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            developer_token=developer_token,
            customer_id=customer_id,
            date_range_days=req.date_range_days,
        )
        return {
            "ok": True,
            "platform": "google_ads",
            "concept_id": req.concept_id,
            "snapshot": {
                "totals": summary["totals"],
                "campaigns_count": len(summary.get("campaigns", [])),
                "ad_groups_count": len(summary.get("ad_groups", [])),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Ads sync failed: {e}")


# ---------------------------------------------------------------------------
# Meta Organic (Instagram + Facebook Page insights) sync
# ---------------------------------------------------------------------------
@router.post("/meta/organic/sync")
def sync_meta_organic_endpoint(req: AdsSyncRequest):
    """Pull Instagram Business organic metrics and store as a platform_data_snapshot."""
    from connectors.meta_organic_connector import sync_meta_organic  # noqa: PLC0415

    creds = _get_creds("meta", concept_id=req.concept_id)
    access_token = creds.get("access_token")
    # ig_user_id is the Instagram Business Account ID (stored by publishing_routes save_credentials)
    instagram_account_id = creds.get("ig_user_id") or creds.get("instagram_account_id") or ""
    page_id = creds.get("page_id") or ""

    if not access_token:
        raise HTTPException(status_code=400, detail="Meta access_token not configured — add it in Settings → Social publishing")
    if not instagram_account_id and not page_id:
        raise HTTPException(
            status_code=400,
            detail="Neither an Instagram Account ID nor a Facebook Page ID is configured — add at least one in Settings → Social publishing → Instagram & Facebook",
        )

    try:
        summary = sync_meta_organic(
            sb=sb,
            concept_id=req.concept_id,
            access_token=access_token,
            instagram_account_id=instagram_account_id,
            page_id=page_id,
            date_range_days=req.date_range_days,
            end_date=req.end_date,
        )
        ig = summary.get("instagram", {})
        totals = ig.get("totals", {})
        return {
            "ok": True,
            "platform": "meta_organic",
            "concept_id": req.concept_id,
            "snapshot": {
                "followers": totals.get("followers"),
                "posts_in_range": totals.get("posts_in_range"),
                "likes": totals.get("likes"),
                "comments": totals.get("comments"),
                "reach": totals.get("reach"),
                "engagement_rate_pct": totals.get("engagement_rate_pct"),
                "range": summary["range"],
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meta organic sync failed: {e}")


# ---------------------------------------------------------------------------
# Post comments (fetched live on demand — not stored, only the count is)
# ---------------------------------------------------------------------------
@router.get("/meta/organic/comments")
def get_post_comments_endpoint(post_id: str, platform: str, concept_id: str):
    """Fetch a post's actual comment text from Meta, live. social_posts only
    stores comments_count from the sync — the text is pulled here on demand
    when a user opens a post's detail view, avoiding an extra API call per
    post on every sync for comments most posts' viewers never open."""
    from connectors.meta_organic_connector import GRAPH_BASE, _get  # noqa: PLC0415

    creds = _get_creds("meta", concept_id=concept_id)
    access_token = creds.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Meta access_token not configured")

    try:
        token = _resolve_meta_token(access_token, platform, creds.get("page_id") or "")
        if platform == "facebook":
            resp = _get(f"{GRAPH_BASE}/{post_id}/comments", {
                "access_token": token,
                "fields": "message,from,created_time",
            })
            comments = [
                {
                    "id": c.get("id"),
                    "text": c.get("message"),
                    "author": (c.get("from") or {}).get("name"),
                    "timestamp": c.get("created_time"),
                }
                for c in resp.get("data", [])
            ]
        else:
            resp = _get(f"{GRAPH_BASE}/{post_id}/comments", {
                "access_token": token,
                "fields": "text,username,timestamp",
            })
            comments = [
                {
                    "id": c.get("id"),
                    "text": c.get("text"),
                    "author": c.get("username"),
                    "timestamp": c.get("timestamp"),
                }
                for c in resp.get("data", [])
            ]
        return {"ok": True, "comments": comments}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch comments: {e}")


def _resolve_meta_token(access_token: str, platform: str, page_id: str) -> str:
    """Facebook writes/reads need a page-scoped token; Instagram works with
    the plain user token."""
    if platform != "facebook" or not page_id:
        return access_token
    from connectors.meta_organic_connector import GRAPH_BASE, _get  # noqa: PLC0415

    try:
        accounts_resp = _get(f"{GRAPH_BASE}/me/accounts", {
            "access_token": access_token,
            "fields": "id,access_token",
        })
        for acct in accounts_resp.get("data", []):
            if str(acct.get("id")) == str(page_id):
                return acct["access_token"]
    except Exception:  # noqa: BLE001
        pass
    return access_token


class CommentReplyRequest(BaseModel):
    comment_id: str
    platform: str
    concept_id: str
    message: str


@router.post("/meta/organic/comments/reply")
def reply_to_comment_endpoint(req: CommentReplyRequest):
    """Post a real, public reply to a comment on Instagram or Facebook."""
    import httpx  # noqa: PLC0415
    from connectors.meta_organic_connector import GRAPH_BASE  # noqa: PLC0415

    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Reply message can't be empty")

    creds = _get_creds("meta", concept_id=req.concept_id)
    access_token = creds.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Meta access_token not configured")

    token = _resolve_meta_token(access_token, req.platform, creds.get("page_id") or "")
    # Instagram replies live on their own /replies edge; Facebook replies
    # are posted as a normal comment nested under the parent comment.
    url = (
        f"{GRAPH_BASE}/{req.comment_id}/replies"
        if req.platform != "facebook"
        else f"{GRAPH_BASE}/{req.comment_id}/comments"
    )

    try:
        resp = httpx.post(url, data={"access_token": token, "message": req.message}, timeout=30.0)
        if not resp.is_success:
            raise RuntimeError(f"Meta Graph API {resp.status_code}: {resp.text[:400]}")
        return {"ok": True, "reply": resp.json()}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to post reply: {e}")


class SuggestReplyRequest(BaseModel):
    comment_text: str
    comment_author: str = ""
    post_caption: str = ""


SUGGEST_REPLY_SYSTEM_PROMPT = (
    "You are replying, as Artcaffe — a premium café brand in Nairobi, Kenya — to a "
    "customer comment on one of our social media posts. Write ONE short, warm, "
    "on-brand reply (1-2 sentences). Sound like a real person from the brand, not "
    "corporate or generic. Use at most one emoji, and only if it fits naturally. "
    "No hashtags. If the comment is a complaint or negative, be sincere and "
    "helpful rather than dismissive — invite them to reach out directly for "
    "anything that needs follow-up (e.g. a specific branch issue). "
    "Output ONLY the reply text — no quotes, no preamble, nothing else."
)


@router.post("/meta/organic/comments/suggest-reply")
def suggest_comment_reply_endpoint(req: SuggestReplyRequest):
    """Draft a suggested reply to a comment with Claude — the user can edit
    it before sending, nothing is posted from this endpoint."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    if not req.comment_text.strip():
        raise HTTPException(status_code=400, detail="No comment text to reply to")

    import anthropic  # noqa: PLC0415

    user_parts = []
    if req.post_caption:
        user_parts.append(f"POST CAPTION:\n{req.post_caption}")
    author = req.comment_author or "a customer"
    user_parts.append(f"COMMENT (from {author}):\n{req.comment_text}")
    user_parts.append("Write a reply.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=20.0)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SUGGEST_REPLY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        text = text.strip('"').strip()
        return {"ok": True, "reply": text}
    except anthropic.APIStatusError as e:
        msg = str(getattr(e, "message", "") or e)
        detail = "Anthropic API credit balance is too low." if "credit balance" in msg.lower() else f"Claude API error: {msg[:300]}"
        raise HTTPException(status_code=400, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate reply: {e}")

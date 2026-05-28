"""
linkedin_publisher.py
---------------------
Post to a LinkedIn Organization page via LinkedIn Marketing API v2.

Credentials needed:
  access_token  — OAuth2 access token with r_liteprofile, w_organization_social scopes
  org_id        — LinkedIn Organization/Company numeric ID
"""
from __future__ import annotations

from typing import Optional
import httpx

LI_API = "https://api.linkedin.com/v2"


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": "202401",
    }


def _handle(resp: httpx.Response, context: str) -> dict | str:
    if not resp.is_success:
        try:
            msg = resp.json().get("message", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        raise RuntimeError(f"LinkedIn {context}: {resp.status_code} — {msg}")
    # 201 Created has no body for ugcPosts
    try:
        return resp.json()
    except Exception:
        return {}


def _upload_image(*, access_token: str, org_urn: str, image_url: str) -> str:
    """Upload an image to LinkedIn Assets API. Returns the asset URN."""
    headers = _headers(access_token)
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        # Register upload
        reg = c.post(
            f"{LI_API}/assets?action=registerUpload",
            headers=headers,
            json={
                "registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": org_urn,
                    "serviceRelationships": [
                        {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                    ],
                }
            },
        )
        _handle(reg, "register upload")
        reg_data = reg.json()
        upload_url = reg_data["value"]["uploadMechanism"][
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
        ]["uploadUrl"]
        asset_urn = reg_data["value"]["asset"]

        # Download source image
        img_resp = c.get(image_url)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

        # Upload binary
        up = c.put(upload_url, content=img_resp.content, headers={"Content-Type": content_type})
        if not up.is_success:
            raise RuntimeError(f"LinkedIn image upload failed: {up.status_code}")

    return asset_urn


def post_linkedin(
    *,
    org_id: str,
    access_token: str,
    text: str,
    image_url: Optional[str] = None,
) -> dict:
    """Post an update (with optional image) to a LinkedIn Organization page."""
    org_urn = f"urn:li:organization:{org_id}"
    headers = _headers(access_token)

    if image_url:
        asset_urn = _upload_image(access_token=access_token, org_urn=org_urn, image_url=image_url)
        payload = {
            "author": org_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media": [{"status": "READY", "description": {"text": text[:200]}, "media": asset_urn}],
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
    else:
        payload = {
            "author": org_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{LI_API}/ugcPosts", headers=headers, json=payload)
        _handle(r, "create post")
        post_id = r.headers.get("x-linkedin-id") or r.headers.get("X-RestLi-Id") or ""

    post_url = f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{post_id}/" if post_id else ""
    return {"platform": "linkedin", "post_id": post_id, "post_url": post_url}


def test_credentials(*, access_token: str, org_id: str) -> dict:
    """Verify credentials by fetching the organization name."""
    with httpx.Client(timeout=15.0) as c:
        r = c.get(
            f"{LI_API}/organizations/{org_id}",
            headers=_headers(access_token),
            params={"fields": "id,localizedName"},
        )
    _handle(r, "credential test")
    data = r.json()
    name = data.get("localizedName") or data.get("name", {}).get("localized", {})
    if isinstance(name, dict):
        name = next(iter(name.values()), str(org_id))
    return {"ok": True, "account_name": str(name)}

"""
google_ads_publisher.py
-----------------------
Create ads in Google Ads via REST API v18.

Two ad types supported:
  Responsive Search Ad (RSA)     — text only, for Search campaigns
  Responsive Display Ad (RDA)    — image + text, for Display/Performance Max campaigns

Credentials needed (stored in platform_credentials table):
  access_token     — OAuth2 Bearer token
  developer_token  — Google Ads developer token
  customer_id      — Google Ads customer ID (digits only, no dashes)
  ad_group_id      — ID of the ad group to add the ad to
  final_url        — Landing page URL
"""
from __future__ import annotations

import base64
from typing import Optional
import httpx

GADS_API = "https://googleads.googleapis.com/v18"


def _headers(access_token: str, developer_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }


def _handle(resp: httpx.Response, context: str) -> dict:
    if not resp.is_success:
        try:
            body = resp.json()
            msg = str(body.get("error", {}).get("message", resp.text[:300]))
        except Exception:
            msg = resp.text[:300]
        raise RuntimeError(f"Google Ads {context}: {resp.status_code} — {msg}")
    return resp.json()


def _cid(customer_id: str) -> str:
    return customer_id.replace("-", "")


# ---------------------------------------------------------------------------
# Image asset upload
# ---------------------------------------------------------------------------

def upload_image_asset(
    *,
    access_token: str,
    developer_token: str,
    customer_id: str,
    image_bytes: bytes,
    asset_name: str = "Artcaffe Banner",
) -> str:
    """
    Upload an image to Google Ads Asset Library.
    Returns the resource name of the created asset (used in Display Ad creation).
    """
    cid = _cid(customer_id)
    b64 = base64.standard_b64encode(image_bytes).decode()

    payload = {
        "operations": [{
            "create": {
                "name": asset_name,
                "type": "IMAGE",
                "imageAsset": {"data": b64},
            }
        }]
    }
    with httpx.Client(timeout=60.0) as c:
        r = c.post(
            f"{GADS_API}/customers/{cid}/assets:mutate",
            headers=_headers(access_token, developer_token),
            json=payload,
        )
    data = _handle(r, "upload image asset")
    return data.get("results", [{}])[0].get("resourceName", "")


# ---------------------------------------------------------------------------
# Responsive Display Ad (image + text)
# ---------------------------------------------------------------------------

def create_responsive_display_ad(
    *,
    access_token: str,
    developer_token: str,
    customer_id: str,
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    marketing_image_resource: str,
    square_image_resource: Optional[str] = None,
    business_name: str = "Artcaffe",
    logo_resource: Optional[str] = None,
) -> dict:
    """
    Create a Responsive Display Ad using a previously uploaded image asset.
    marketing_image_resource — resource name from upload_image_asset() (landscape 1.91:1)
    square_image_resource    — optional square image (1:1), falls back to marketing image
    """
    cid = _cid(customer_id)

    safe_headlines     = [h[:30]  for h in headlines[:5]]
    safe_descriptions  = [d[:90]  for d in descriptions[:5]]
    safe_business_name = business_name[:25]

    while len(safe_headlines)    < 1: safe_headlines.append("Artcaffe Café")
    while len(safe_descriptions) < 1: safe_descriptions.append("Premium café experience in Nairobi.")

    sq_resource = square_image_resource or marketing_image_resource
    ad_group_resource = f"customers/{cid}/adGroups/{ad_group_id}"

    marketing_images = [{"asset": marketing_image_resource, "fieldType": "MARKETING_IMAGE"}]
    square_images    = [{"asset": sq_resource,               "fieldType": "SQUARE_MARKETING_IMAGE"}]
    logos            = [{"asset": logo_resource,             "fieldType": "LOGO"}] if logo_resource else []

    payload = {
        "operations": [{
            "create": {
                "adGroup": ad_group_resource,
                "status": "ENABLED",
                "ad": {
                    "responsiveDisplayAd": {
                        "headlines":         [{"text": h} for h in safe_headlines],
                        "descriptions":      [{"text": d} for d in safe_descriptions],
                        "businessName":      safe_business_name,
                        "marketingImages":   marketing_images,
                        "squareMarketingImages": square_images,
                        **({"logos": logos} if logos else {}),
                        "finalUrls":         [final_url],
                    },
                    "finalUrls": [final_url],
                },
            }
        }]
    }

    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{GADS_API}/customers/{cid}/adGroupAds:mutate",
            headers=_headers(access_token, developer_token),
            json=payload,
        )
    data = _handle(r, "create responsive display ad")
    resource_name = data.get("results", [{}])[0].get("resourceName", "")
    return {
        "platform": "google_ads",
        "ad_type": "responsive_display",
        "resource_name": resource_name,
        "post_url": f"https://ads.google.com/aw/ads?customerId={cid}",
    }


# ---------------------------------------------------------------------------
# Responsive Search Ad (text only)
# ---------------------------------------------------------------------------

def create_responsive_search_ad(
    *,
    access_token: str,
    developer_token: str,
    customer_id: str,
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
) -> dict:
    """
    Create a Responsive Search Ad (text only) in the specified ad group.
    Kept for Search campaigns where no image is available.
    """
    cid = _cid(customer_id)

    safe_headlines    = [h[:30] for h in headlines[:15]]
    safe_descriptions = [d[:90] for d in descriptions[:4]]

    while len(safe_headlines)    < 3: safe_headlines.append("Visit Us Today")
    while len(safe_descriptions) < 2: safe_descriptions.append("Premium café experience in Nairobi.")

    ad_group_resource = f"customers/{cid}/adGroups/{ad_group_id}"

    payload = {
        "operations": [{
            "create": {
                "adGroup": ad_group_resource,
                "status": "ENABLED",
                "ad": {
                    "responsiveSearchAd": {
                        "headlines":     [{"text": h} for h in safe_headlines],
                        "descriptions":  [{"text": d} for d in safe_descriptions],
                    },
                    "finalUrls": [final_url],
                },
            }
        }]
    }

    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{GADS_API}/customers/{cid}/adGroupAds:mutate",
            headers=_headers(access_token, developer_token),
            json=payload,
        )
    data = _handle(r, "create RSA")
    resource_name = data.get("results", [{}])[0].get("resourceName", "")
    return {
        "platform": "google_ads",
        "ad_type": "responsive_search",
        "resource_name": resource_name,
        "post_url": f"https://ads.google.com/aw/ads?customerId={cid}",
    }


# ---------------------------------------------------------------------------
# Credentials test
# ---------------------------------------------------------------------------

def test_credentials(*, access_token: str, developer_token: str, customer_id: str) -> dict:
    cid = _cid(customer_id)
    with httpx.Client(timeout=15.0) as c:
        r = c.get(
            f"{GADS_API}/customers/{cid}",
            headers=_headers(access_token, developer_token),
            params={"fieldMask": "descriptiveName,id"},
        )
    data = _handle(r, "credential test")
    return {"ok": True, "account_name": data.get("descriptiveName") or f"Customer {cid}"}

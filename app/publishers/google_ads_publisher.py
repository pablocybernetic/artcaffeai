"""
google_ads_publisher.py
-----------------------
Create a Responsive Search Ad in an existing Google Ads ad group via REST API v18.

Credentials needed:
  access_token     — OAuth2 Bearer token (from service account or user OAuth)
  developer_token  — Google Ads developer token (from Google Ads account)
  customer_id      — Google Ads customer ID (digits only, no dashes)
  campaign_id      — ID of the campaign to add the ad to
  ad_group_id      — ID of the ad group within that campaign
  final_url        — Landing page URL for the ad
"""
from __future__ import annotations

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
    Create a Responsive Search Ad (RSA) in the specified ad group.

    Google Ads requires:
      - 3–15 headlines (≤ 30 chars each)
      - 2–4 descriptions (≤ 90 chars each)
      - At least one final URL
    """
    cid = customer_id.replace("-", "")

    # Truncate to platform limits
    safe_headlines = [h[:30] for h in headlines[:15]]
    safe_descriptions = [d[:90] for d in descriptions[:4]]

    # Pad if below minimum
    while len(safe_headlines) < 3:
        safe_headlines.append("Visit Us Today")
    while len(safe_descriptions) < 2:
        safe_descriptions.append("Premium café experience in Nairobi.")

    ad_group_resource = f"customers/{cid}/adGroups/{ad_group_id}"

    payload = {
        "operations": [
            {
                "create": {
                    "adGroup": ad_group_resource,
                    "status": "ENABLED",
                    "ad": {
                        "responsiveSearchAd": {
                            "headlines": [{"text": h} for h in safe_headlines],
                            "descriptions": [{"text": d} for d in safe_descriptions],
                        },
                        "finalUrls": [final_url],
                    },
                }
            }
        ]
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
        "resource_name": resource_name,
        "post_url": f"https://ads.google.com/aw/ads?customerId={cid}",
    }


def test_credentials(*, access_token: str, developer_token: str, customer_id: str) -> dict:
    """Verify credentials by listing accessible customers."""
    cid = customer_id.replace("-", "")
    with httpx.Client(timeout=15.0) as c:
        r = c.get(
            f"{GADS_API}/customers/{cid}",
            headers=_headers(access_token, developer_token),
            params={"fieldMask": "descriptiveName,id"},
        )
    data = _handle(r, "credential test")
    return {"ok": True, "account_name": data.get("descriptiveName") or f"Customer {cid}"}

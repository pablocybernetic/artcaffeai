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

from typing import Optional
import httpx

GRAPH = "https://graph.facebook.com/v21.0"


def _handle(resp: httpx.Response, context: str) -> dict:
    if not resp.is_success:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        raise RuntimeError(f"Meta {context}: {resp.status_code} — {msg}")
    return resp.json()


def _page_token(page_id: str, access_token: str, client: httpx.Client) -> str:
    """
    Exchange a System User token for a Page Access Token.

    The Graph API requires a Page Access Token (not a System User token) for
    posting to pages. System User tokens have all permissions but the subject
    must be the page itself for page write operations.

    Falls back to the original token if the exchange fails (e.g. the stored
    token is already a Page Access Token).
    """
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


def post_instagram(
    *,
    ig_user_id: str,
    access_token: str,
    image_url: str,
    caption: str,
) -> dict:
    """Two-step IG publish: create container → publish container."""
    with httpx.Client(timeout=45.0) as c:
        # Step 1 — create media container
        r1 = c.post(
            f"{GRAPH}/{ig_user_id}/media",
            params={"image_url": image_url, "caption": caption, "access_token": access_token},
        )
        creation_id = _handle(r1, "create container")["id"]

        # Step 2 — publish
        r2 = c.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": creation_id, "access_token": access_token},
        )
        post_id = _handle(r2, "publish container")["id"]

    post_url = f"https://www.instagram.com/p/{post_id}/"
    return {"platform": "instagram", "post_id": post_id, "post_url": post_url}


def post_facebook(
    *,
    page_id: str,
    access_token: str,
    message: str,
    image_url: Optional[str] = None,
) -> dict:
    """Post a photo or text update to a Facebook Page using a Page Access Token."""
    with httpx.Client(timeout=45.0) as c:
        # Exchange System User token → Page Access Token (required for page write ops)
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

    post_url = f"https://www.facebook.com/{post_id}"
    return {"platform": "facebook", "post_id": post_id, "post_url": post_url}


def test_credentials(*, access_token: str, page_id: str) -> dict:
    """
    Verify token is valid and the Page Access Token exchange works.
    Uses /me to confirm the token, then exchanges for a Page Access Token
    and fetches the page name to confirm posting rights.
    """
    with httpx.Client(timeout=15.0) as c:
        # Confirm the token is alive
        r_me = c.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": access_token})
        _handle(r_me, "credential test")

        # Exchange for Page Access Token and fetch the page name
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
            # Fall back to /me/accounts list
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

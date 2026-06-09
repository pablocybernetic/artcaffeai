"""
whatsapp_publisher.py
---------------------
Post to WhatsApp Business via the Meta Cloud API (free tier).

Highly relevant for Kenya — WhatsApp is the #1 messaging platform used by
Artcaffe customers for promotions, events, and loyalty engagement.

Credentials stored in platform_credentials (platform='whatsapp'):
  access_token  — Cloud API permanent token (from Meta Business → WhatsApp → API Setup)
  ig_user_id    — Phone Number ID (NOT the actual phone number)
  page_id       — Recipient phone number in E.164 format, e.g. "+254712345678"
                  (for broadcasts, store the primary group/channel number)
  account_name  — display name e.g. "Artcaffe Business"

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/messages
"""
from __future__ import annotations

from typing import Optional
import httpx

GRAPH_API = "https://graph.facebook.com/v19.0"


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def post_whatsapp(
    phone_number_id: str,
    access_token: str,
    to_number: str,
    text: str,
    image_url: Optional[str] = None,
) -> dict:
    """
    Send a message to a WhatsApp number via the Cloud API.

    If image_url is provided, sends an image message with text as the caption.
    Falls back to text-only if the image send fails.
    """
    url = f"{GRAPH_API}/{phone_number_id}/messages"
    headers = _headers(access_token)

    if image_url:
        body = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": text[:1024],
            },
        }
        resp = httpx.post(url, json=body, headers=headers, timeout=20)
        if resp.is_success:
            data = resp.json()
            msg_id = (data.get("messages") or [{}])[0].get("id")
            return {"post_id": msg_id, "post_url": None}
        # Fall through to text-only on image send failure
        print(f"[whatsapp_publisher] image send failed ({resp.status_code}), retrying as text", flush=True)

    # Text-only message
    body = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text[:4096], "preview_url": True},
    }
    resp = httpx.post(url, json=body, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    msg_id = (data.get("messages") or [{}])[0].get("id")
    return {"post_id": msg_id, "post_url": None}


def test_credentials(phone_number_id: str, access_token: str) -> dict:
    """Verify credentials by fetching the phone number metadata."""
    resp = httpx.get(
        f"{GRAPH_API}/{phone_number_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "ok": True,
        "account_name": data.get("display_phone_number") or data.get("verified_name") or "WhatsApp Business",
    }

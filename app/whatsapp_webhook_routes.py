"""
whatsapp_webhook_routes.py
----------------------------
WhatsApp Business Cloud API inbound webhook (Phase 2) — receives customer
replies and delivery/read status callbacks for our own outbound sends.

NOT behind require_api_key — Meta's servers call this directly and don't
have our API key. Authenticated instead by Meta's own webhook security
model: the GET handshake's hub.verify_token, and the POST body's
X-Hub-Signature-256 HMAC (verified against the raw request bytes before
any JSON parsing, since the signature covers the exact wire body).

Prefix: /whatsapp
Endpoints:
  GET  /whatsapp/webhook  — verification handshake
  POST /whatsapp/webhook  — inbound messages + status callbacks
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from supabase import Client, create_client

import notification_service
from secrets_crypto import decrypt_value

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/whatsapp")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_whatsapp_secret_fields() -> dict[str, Optional[str]]:
    res = (
        sb.table("platform_credentials")
        .select("extra_json")
        .eq("platform", "whatsapp")
        .eq("is_active", True)
        .is_("concept_id", "null")
        .maybe_single()
        .execute()
    )
    extra = ((res.data or {}) if res else {}).get("extra_json") or {}
    verify_token = None
    app_secret = None
    try:
        if extra.get("webhook_verify_token_enc"):
            verify_token = decrypt_value(extra["webhook_verify_token_enc"])
    except Exception:  # noqa: BLE001
        pass
    try:
        if extra.get("app_secret_enc"):
            app_secret = decrypt_value(extra["app_secret_enc"])
    except Exception:  # noqa: BLE001
        pass
    return {"verify_token": verify_token, "app_secret": app_secret}


@router.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    secrets = _get_whatsapp_secret_fields()
    if mode == "subscribe" and secrets["verify_token"] and token == secrets["verify_token"]:
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(403, "Verification failed")


@router.post("/webhook")
async def receive_webhook(request: Request, bg: BackgroundTasks):
    raw_body = await request.body()
    secrets = _get_whatsapp_secret_fields()

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if secrets["app_secret"]:
        expected = "sha256=" + hmac.new(secrets["app_secret"].encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            raise HTTPException(401, "Invalid signature")
    else:
        # No app secret configured yet — accept but flag loudly rather than
        # silently drop all webhook traffic during initial setup, since an
        # admin still mid-configuring credentials needs to see this in logs.
        print("[whatsapp_webhook] WARNING: no app secret configured — signature not verified", flush=True)

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                contact_id = _store_inbound_message(msg)
                # Backgrounded so Meta's webhook gets a fast response even
                # if the notification email is slow — matches every other
                # notification path in this codebase (table_booking_routes.py
                # etc.), never blocking the thing that actually matters.
                bg.add_task(_notify_admins_of_whatsapp_message, msg, contact_id)
            for status in value.get("statuses", []):
                _apply_status_update(status)

    return {"ok": True}


def _find_contact_id(phone_number: Optional[str]) -> Optional[str]:
    if not phone_number:
        return None
    normalized = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    res = sb.table("whatsapp_contacts").select("id").eq("phone_number", normalized).maybe_single().execute()
    return ((res.data or {}) if res else {}).get("id")


def _store_inbound_message(msg: dict) -> Optional[str]:
    contact_id = _find_contact_id(msg.get("from"))
    body = (msg.get("text") or {}).get("body")
    sb.table("whatsapp_messages").insert({
        "direction": "inbound",
        "contact_id": contact_id,
        "wa_message_id": msg.get("id"),
        "body": body,
        "raw_payload": msg,
        "created_at": _now(),
    }).execute()
    return contact_id


def _notify_admins_of_whatsapp_message(msg: dict, contact_id: Optional[str]) -> None:
    """Best-effort admin notification for a new inbound WhatsApp message —
    same generic team-notification fan-out as table_booking_routes.py's
    _notify_admins_of_booking, so it already respects each admin's own
    Users → Notifications preference for notif_type "whatsapp_message_received".
    Never raises — a notification failure must never affect webhook
    processing or the stored message."""
    try:
        phone = msg.get("from") or "unknown number"
        body = (msg.get("text") or {}).get("body") or "(no text — likely an image, document, or other media)"

        contact_name = None
        if contact_id:
            res = sb.table("whatsapp_contacts").select("full_name").eq("id", contact_id).maybe_single().execute()
            contact_name = ((res.data or {}) if res else {}).get("full_name")

        sender_label = f"{contact_name} ({phone})" if contact_name else phone
        subject = f"Artcaffe — New WhatsApp message from {contact_name or phone}"
        html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe AI Marketing</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">A new WhatsApp message came in from <strong>{sender_label}</strong>:</p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;font-size:14px;color:#374151;font-style:italic;">
      "{body}"
    </div>
    <a href="{notification_service.DASHBOARD_URL}/whatsapp"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      Reply in WhatsApp
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
        notification_service._notify_relevant_team(
            sb,
            notif_type="whatsapp_message_received",
            subject=subject,
            html=html,
            payload={"phone": phone, "contact_id": contact_id, "body": body[:200]},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[whatsapp_webhook_routes] admin notify failed: {exc}", flush=True)


def _apply_status_update(status: dict) -> None:
    wa_message_id = status.get("id")
    new_status = status.get("status")
    if not wa_message_id or new_status not in ("sent", "delivered", "read", "failed"):
        return
    sb.table("whatsapp_campaign_sends").update({"status": new_status}).eq("wa_message_id", wa_message_id).execute()
    errors = status.get("errors") or []
    sb.table("whatsapp_messages").insert({
        "direction": "outbound",
        "wa_message_id": wa_message_id,
        "status": new_status,
        "error_message": errors[0].get("title") if errors else None,
        "raw_payload": status,
        "created_at": _now(),
    }).execute()

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

from fastapi import APIRouter, HTTPException, Request, Response
from supabase import Client, create_client

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
async def receive_webhook(request: Request):
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
                _store_inbound_message(msg)
            for status in value.get("statuses", []):
                _apply_status_update(status)

    return {"ok": True}


def _find_contact_id(phone_number: Optional[str]) -> Optional[str]:
    if not phone_number:
        return None
    normalized = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    res = sb.table("whatsapp_contacts").select("id").eq("phone_number", normalized).maybe_single().execute()
    return ((res.data or {}) if res else {}).get("id")


def _store_inbound_message(msg: dict) -> None:
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

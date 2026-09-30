"""
onfon_sms_connector.py
------------------------
Onfon Media SMS (SendBulkSMS, used here to send exactly one message per
call). Raw httpx, mirrors this codebase's other connectors.

Docs (docs.onfonmedia.co.ke/rest/sms/) show `AccessKey` as a request
HEADER, separate from `ApiKey`/`ClientId` in the JSON body — the sample
cURL the credentials arrived with only showed the body, which would have
401'd on first real send.
"""
from __future__ import annotations

import re

import httpx

SEND_URL = "https://api.onfonmedia.co.ke/v1/sms/SendBulkSMS"


def _normalize_kenyan_phone(phone: str) -> str:
    """Best-effort normalize to E.164 (+254...).

    Onfon's gateway accepts a local '07xxxxxxxx'/'01xxxxxxxx' number with
    ErrorCode 0 (no exception raised here) but the message then silently
    never reaches the handset — customer booking SMS sent with the phone
    exactly as typed on the form (local format) went unnoticed as a
    "sent" failure for this reason, while staff numbers (already stored
    as +254...) delivered fine. Numbers already international, or in an
    unrecognized shape, are passed through unchanged."""
    digits = re.sub(r"[^\d+]", "", phone or "")
    if digits.startswith("+"):
        return digits
    if digits.startswith("254"):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+254{digits[1:]}"
    return digits


def send_sms(
    *,
    to_number: str,
    text: str,
    api_key: str,
    client_id: str,
    access_key: str,
    sender_id: str = "OnfonInfo",
) -> dict:
    resp = httpx.post(
        SEND_URL,
        headers={"Content-Type": "application/json", "AccessKey": access_key},
        json={
            "SenderId": sender_id,
            "MessageParameters": [{"Number": _normalize_kenyan_phone(to_number), "Text": text}],
            "ApiKey": api_key,
            "ClientId": client_id,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("ErrorCode") != 0:
        raise RuntimeError(f"Onfon SMS failed: {data.get('ErrorDescription')}")
    return data

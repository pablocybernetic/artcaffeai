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

import httpx

SEND_URL = "https://api.onfonmedia.co.ke/v1/sms/SendBulkSMS"


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
            "MessageParameters": [{"Number": to_number, "Text": text}],
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

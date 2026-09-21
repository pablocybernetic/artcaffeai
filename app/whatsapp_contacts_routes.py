"""
whatsapp_contacts_routes.py
----------------------------
Contacts/audience + template broadcast campaigns for WhatsApp.

Prefix: /whatsapp
Auth:   X-Api-Key header (require_api_key, module-local copy — every
        route file in this app defines its own, not a shared import)

Endpoints:
  GET    /whatsapp/contacts             — list contacts
  POST   /whatsapp/contacts             — add one contact
  POST   /whatsapp/contacts/bulk        — add many contacts
  PATCH  /whatsapp/contacts/{id}        — edit / toggle opt_in / archive
  GET    /whatsapp/templates            — approved Meta message templates
  POST   /whatsapp/campaigns            — send a template to a set of contacts
  GET    /whatsapp/campaigns/{job_id}   — per-status send counts for a campaign

This is Phase 1 only — no inbound webhook here (see the approved plan's
Phase 2, gated on a DNS record the user has to add first).

WhatsApp credentials are stored globally (concept_id IS NULL) in
platform_credentials, not per-brand — matches how publishing_routes.py
treats whatsapp/linkedin/twitter/google_ads (only meta/tiktok are
per-concept).
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

import job_runner
from publishers.whatsapp_publisher import list_message_templates

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

_PHONE_RE = re.compile(r"^\+\d+$")


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/whatsapp", dependencies=[Depends(require_api_key)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_whatsapp_creds(concept_id: Optional[str] = None) -> dict:
    # concept_id is accepted (not used) — whatsapp credentials are stored
    # globally, unlike meta/tiktok which are per-brand.
    res = (
        sb.table("platform_credentials")
        .select("*")
        .eq("platform", "whatsapp")
        .eq("is_active", True)
        .is_("concept_id", "null")
        .maybe_single()
        .execute()
    )
    creds = res.data if res else None
    if not creds or not creds.get("access_token") or not creds.get("ig_user_id"):
        raise HTTPException(400, "WhatsApp not configured — add the Cloud API Token and Phone Number ID in Settings → WhatsApp credentials")
    return creds


def _upsert_contact(
    concept_id: Optional[str],
    phone_number: str,
    name: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    # NOTE: Postgres treats concept_id IS NULL as distinct per row for the
    # (concept_id, phone_number) unique constraint, so a DB-level upsert
    # with on_conflict wouldn't dedupe global (concept_id=None) contacts —
    # select-then-update-or-insert instead, same pattern as
    # publishing_routes.py's _upsert_platform_credentials.
    q = sb.table("whatsapp_contacts").select("id").eq("phone_number", phone_number)
    q = q.eq("concept_id", concept_id) if concept_id else q.is_("concept_id", "null")
    existing = q.maybe_single().execute()

    if existing is not None and existing.data:
        update: dict[str, Any] = {"updated_at": _now()}
        if name is not None:
            update["name"] = name
        if tags is not None:
            update["tags"] = tags
        sb.table("whatsapp_contacts").update(update).eq("id", existing.data["id"]).execute()
        return {"id": existing.data["id"], "created": False}

    insert_row = {
        "concept_id": concept_id,
        "phone_number": phone_number,
        "name": name,
        "tags": tags or [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = sb.table("whatsapp_contacts").insert(insert_row).execute()
    new_id = (res.data or [{}])[0].get("id")
    return {"id": new_id, "created": True}


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
class ContactIn(BaseModel):
    phone_number: str
    name: Optional[str] = None
    concept_id: Optional[str] = None
    tags: list[str] = []


class BulkContactItem(BaseModel):
    phone_number: str
    name: Optional[str] = None


class BulkContactsIn(BaseModel):
    contacts: list[BulkContactItem]
    concept_id: Optional[str] = None


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    opt_in: Optional[bool] = None
    tags: Optional[list[str]] = None


@router.get("/contacts")
def list_contacts(concept_id: Optional[str] = None):
    q = sb.table("whatsapp_contacts").select("*").order("created_at", desc=True)
    if concept_id:
        q = q.eq("concept_id", concept_id)
    res = q.execute()
    return {"ok": True, "contacts": res.data or []}


@router.post("/contacts")
def add_contact(body: ContactIn):
    if not _PHONE_RE.match(body.phone_number):
        raise HTTPException(400, "phone_number must be E.164 format, e.g. +254712345678")
    result = _upsert_contact(body.concept_id, body.phone_number, name=body.name, tags=body.tags)
    return {"ok": True, **result}


@router.post("/contacts/bulk")
def bulk_add_contacts(body: BulkContactsIn):
    added = 0
    skipped = 0
    errors: list[dict] = []
    for contact in body.contacts:
        if not _PHONE_RE.match(contact.phone_number):
            skipped += 1
            errors.append({"phone_number": contact.phone_number, "reason": "not E.164 format (must start with + and be all digits)"})
            continue
        try:
            _upsert_contact(body.concept_id, contact.phone_number, name=contact.name)
            added += 1
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            errors.append({"phone_number": contact.phone_number, "reason": str(exc)[:200]})
    return {"added": added, "skipped": skipped, "errors": errors}


@router.patch("/contacts/{contact_id}")
def update_contact(contact_id: str, body: ContactUpdate):
    existing = sb.table("whatsapp_contacts").select("opt_in").eq("id", contact_id).maybe_single().execute()
    if not existing or not existing.data:
        raise HTTPException(404, "Contact not found")

    update: dict[str, Any] = {}
    if body.name is not None:
        update["name"] = body.name
    if body.tags is not None:
        update["tags"] = body.tags
    if body.opt_in is not None:
        update["opt_in"] = body.opt_in
        if body.opt_in and not existing.data.get("opt_in"):
            update["opted_in_at"] = _now()

    if not update:
        raise HTTPException(400, "No fields to update")
    update["updated_at"] = _now()
    res = sb.table("whatsapp_contacts").update(update).eq("id", contact_id).execute()
    return {"ok": True, "contact": (res.data or [None])[0]}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@router.get("/templates")
def get_templates(concept_id: Optional[str] = None):
    creds = _get_whatsapp_creds(concept_id)
    waba_id = creds.get("org_id")
    if not waba_id:
        raise HTTPException(400, "WhatsApp Business Account ID (WABA ID) not configured — add it in Settings → WhatsApp credentials")
    templates = list_message_templates(waba_id=waba_id, access_token=creds["access_token"])
    return {"ok": True, "templates": templates}


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
class CampaignIn(BaseModel):
    template_name: str
    language_code: str
    body_params: list[str] = []
    contact_ids: Optional[list[str]] = None
    tag: Optional[str] = None
    concept_id: Optional[str] = None


@router.post("/campaigns")
def create_campaign(body: CampaignIn, background_tasks: BackgroundTasks):
    if bool(body.contact_ids) == bool(body.tag):
        raise HTTPException(400, "Provide exactly one of contact_ids or tag")

    # Always filter to opt_in = true, even for explicit contact_ids — never
    # send to a non-opted-in contact regardless of how it was selected.
    q = sb.table("whatsapp_contacts").select("id").eq("opt_in", True)
    q = q.in_("id", body.contact_ids) if body.contact_ids else q.contains("tags", [body.tag])
    contacts_res = q.execute()
    contact_rows = contacts_res.data or []
    if not contact_rows:
        raise HTTPException(400, "No opted-in contacts matched — nothing to send")

    job_id = str(uuid.uuid4())
    sb.table("jobs").insert({
        "id": job_id,
        "agent_type": "whatsapp_campaign_send",
        "status": "pending",
        "input_payload": {
            "template_name": body.template_name,
            "language_code": body.language_code,
            "body_params": body.body_params,
            "concept_id": body.concept_id,
        },
        "created_at": _now(),
        "updated_at": _now(),
    }).execute()

    send_rows = [
        {
            "job_id": job_id,
            "contact_id": contact["id"],
            "template_name": body.template_name,
            "status": "pending",
            "created_at": _now(),
        }
        for contact in contact_rows
    ]
    sb.table("whatsapp_campaign_sends").insert(send_rows).execute()

    # Fires once, inline — no recurring scheduler needed (see job_runner.py's
    # "Inline (from FastAPI BackgroundTasks)" usage note).
    background_tasks.add_task(job_runner.run_job, job_id)

    return {"job_id": job_id, "contact_count": len(contact_rows)}


@router.get("/campaigns/{job_id}")
def get_campaign_status(job_id: str):
    res = sb.table("whatsapp_campaign_sends").select("status").eq("job_id", job_id).execute()
    rows = res.data or []
    counts = {"pending": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0}
    for row in rows:
        status = row.get("status")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(rows)
    return counts

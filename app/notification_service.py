"""
notification_service.py
-----------------------
Notification layer for the Artcaffe AI Marketing System.

Two stores are written on every notification:
  1. `notifications` table  — team-facing inbox (per-recipient rows, future UI bell).
  2. `agent_notifications`  — system audit log (one row per event, no recipient FK).

Email delivery is attempted via Resend when RESEND_API_KEY is set.

Env vars:
  RESEND_API_KEY         — Resend API key (email disabled if not set)
  NOTIFY_FROM_EMAIL      — sender address (default: noreply@artcaffemarket.co.ke)
  DASHBOARD_URL          — base URL for dashboard links in emails
                           (default: https://marketing.artcaffe.co.ke)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client

RESEND_API_KEY   = os.environ.get("RESEND_API_KEY")
FROM_EMAIL       = os.environ.get("NOTIFY_FROM_EMAIL", "noreply@artcaffemarket.co.ke")
DASHBOARD_URL    = os.environ.get("DASHBOARD_URL", "https://marketing.artcaffe.co.ke")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_email(to_email: str, subject: str, html: str) -> bool:
    """Fire-and-forget Resend email. Returns True on success."""
    if not RESEND_API_KEY:
        return False
    try:
        import resend  # type: ignore
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notification_service] email failed to={to_email}: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Low-level write helpers
# ---------------------------------------------------------------------------

def _write_agent_notification(sb: Client, type: str, payload: dict) -> str:
    """Append a row to agent_notifications (system audit log). Returns id."""
    row_id = str(uuid.uuid4())
    sb.table("agent_notifications").insert({
        "id":         row_id,
        "type":       type,
        "payload":    payload,
        "created_at": _now(),
    }).execute()
    return row_id


def _write_team_notification(
    sb: Client,
    *,
    recipient_id: str,
    subject: str,
    body: str,
    brief_id: Optional[str] = None,
    content_item_id: Optional[str] = None,
    job_id: Optional[str] = None,
    channel: str = "email",
) -> str:
    """Insert one row into the notifications table. Returns id."""
    row: dict[str, Any] = {
        "recipient_id": recipient_id,
        "channel":      channel,
        "subject":      subject,
        "body":         body,
        "created_at":   _now(),
    }
    if brief_id:
        row["brief_id"] = brief_id
    if content_item_id:
        row["content_item_id"] = content_item_id
    if job_id:
        row["job_id"] = job_id

    res = sb.table("notifications").insert(row).execute()
    return res.data[0]["id"] if res.data else ""


def _mark_notification_sent(sb: Client, notif_id: str) -> None:
    if notif_id:
        sb.table("notifications").update({"sent_at": _now()}).eq("id", notif_id).execute()


# ---------------------------------------------------------------------------
# Public API — system events (no specific recipient)
# ---------------------------------------------------------------------------

def send_notification(
    sb: Client,
    *,
    type: str,
    subject: str,
    html: str,
    payload: Optional[dict] = None,
    user_id: Optional[str] = None,
    to_email: Optional[str] = None,
) -> bool:
    """
    Write to agent_notifications and optionally send an email via Resend.
    Used for system events (ideation/production complete) where there is no
    specific team_members recipient.

    Returns True if the email was sent successfully.
    """
    _write_agent_notification(sb, type, {**(payload or {}), "user_id": user_id})

    recipient = to_email or os.environ.get("NOTIFY_TO_EMAIL", "pgitau@artcaffe.co.ke")
    sent = _send_email(recipient, subject, html)

    if not sent:
        print(
            f"[notification_service] Email skipped (no RESEND_API_KEY). "
            f"type={type} subject='{subject}'",
            flush=True,
        )
    else:
        print(f"[notification_service] Sent. type={type} to={recipient}", flush=True)

    return sent


# ---------------------------------------------------------------------------
# Public API — team notifications (with recipient)
# ---------------------------------------------------------------------------

def notify_team(
    sb: Client,
    *,
    recipient_id: str,
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html: str,
    brief_id: Optional[str] = None,
    content_item_id: Optional[str] = None,
    job_id: Optional[str] = None,
    type: str = "notification",
    payload: Optional[dict] = None,
) -> bool:
    """
    Write to notifications (per-recipient row) + agent_notifications (audit log)
    and send an email if Resend is configured.

    Returns True if the email was sent.
    """
    # 1. Team inbox row
    notif_id = _write_team_notification(
        sb,
        recipient_id=recipient_id,
        subject=subject,
        body=html,
        brief_id=brief_id,
        content_item_id=content_item_id,
        job_id=job_id,
    )

    # 2. System audit log
    _write_agent_notification(sb, type, {
        **(payload or {}),
        "recipient_id":    recipient_id,
        "recipient_email": recipient_email,
    })

    # 3. Email
    sent = _send_email(recipient_email, subject, html)
    if sent and notif_id:
        _mark_notification_sent(sb, notif_id)

    return sent


def notify_approval_needed_to_team(
    sb: Client,
    *,
    brief_id: str,
    title: str,
) -> int:
    """
    Notify every active admin and content_manager that a content item
    has moved to pending_review and needs their approval.

    Writes one notifications row per recipient and sends emails.
    Returns the number of emails successfully delivered.
    """
    members_res = (
        sb.table("team_members")
        .select("id,email,full_name")
        .in_("role", ["admin", "content_manager"])
        .eq("is_active", True)
        .execute()
    )
    members = members_res.data or []

    sent = 0
    for m in members:
        subject = f"Artcaffe — Approval needed: {title}"
        html = (
            f"<p>Hi {m['full_name']},</p>"
            f"<p>The content item <strong>{title}</strong> is awaiting your approval.</p>"
            f"<p><a href='{DASHBOARD_URL}/briefs' style='background:#1a1a1a;color:#fff;"
            f"padding:8px 16px;border-radius:6px;text-decoration:none;'>Review in dashboard</a></p>"
            f"<p>— Artcaffe AI</p>"
        )
        ok = notify_team(
            sb,
            recipient_id=m["id"],
            recipient_email=m["email"],
            recipient_name=m["full_name"],
            subject=subject,
            html=html,
            brief_id=brief_id,
            type="approval_needed",
            payload={"brief_id": brief_id, "title": title},
        )
        if ok:
            sent += 1

    print(
        f"[notification_service] approval_needed fired. brief={brief_id} "
        f"recipients={len(members)} emails_sent={sent}",
        flush=True,
    )
    return sent


# ---------------------------------------------------------------------------
# Convenience helpers (backward-compatible)
# ---------------------------------------------------------------------------

def notify_ideation_complete(sb: Client, brief_id: str, n_ideas: int) -> bool:
    return send_notification(
        sb,
        type="ideation_complete",
        subject=f"Artcaffe AI — {n_ideas} new content ideas ready for review",
        html=(
            f"<p>Hello,</p>"
            f"<p>The ideation agent has generated <strong>{n_ideas} content ideas</strong> "
            f"for brief <code>{brief_id}</code>.</p>"
            f"<p><a href='{DASHBOARD_URL}/briefs'>Review in dashboard</a></p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id, "n_ideas": n_ideas},
    )


def notify_production_complete(sb: Client, brief_id: str) -> bool:
    return send_notification(
        sb,
        type="production_complete",
        subject="Artcaffe AI — Final production copy is ready",
        html=(
            f"<p>Hello,</p>"
            f"<p>The production agent has finished writing publication-ready copy "
            f"for brief <code>{brief_id}</code>.</p>"
            f"<p><a href='{DASHBOARD_URL}/briefs'>Review in dashboard</a></p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id},
    )


def notify_approval_needed(sb: Client, brief_id: str, title: str) -> bool:
    """Legacy single-recipient helper. Prefer notify_approval_needed_to_team."""
    return send_notification(
        sb,
        type="approval_needed",
        subject=f"Artcaffe AI — Approval needed: {title}",
        html=(
            f"<p>Hello,</p>"
            f"<p>Content item <strong>{title}</strong> is awaiting approval "
            f"(brief <code>{brief_id}</code>).</p>"
            f"<p><a href='{DASHBOARD_URL}/briefs'>Review in dashboard</a></p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id, "title": title},
    )

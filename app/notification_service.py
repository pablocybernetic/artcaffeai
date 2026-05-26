"""
notification_service.py
-----------------------
Lightweight notification layer for the Artcaffe AI Marketing System.

Responsibilities:
  - Insert a row into the `agent_notifications` table (always).
  - Optionally send an email via the Resend API (if RESEND_API_KEY is set).

Env vars:
  RESEND_API_KEY         — Resend API key (email disabled if not set)
  NOTIFY_FROM_EMAIL      — sender address (default: noreply@artcaffe.co.ke)
  NOTIFY_TO_EMAIL        — default recipient (default: pgitau@artcaffe.co.ke)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from supabase import Client

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
FROM_EMAIL = os.environ.get("NOTIFY_FROM_EMAIL", "noreply@artcaffe.co.ke")
NOTIFY_TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", "pgitau@artcaffe.co.ke")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    Insert a notification record and optionally send an email via Resend.

    Returns True if the email was sent successfully, False otherwise
    (including when email is disabled because RESEND_API_KEY is not set).
    """
    # Always persist to the agent_notifications table
    notif_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": type,
        "payload": payload or {},
        "created_at": _now(),
    }
    insert_res = sb.table("agent_notifications").insert(notif_row).execute()
    notif_id: Optional[str] = None
    if insert_res.data:
        notif_id = insert_res.data[0].get("id", notif_row["id"])

    # If no API key, log and return early
    if not RESEND_API_KEY:
        print(
            f"[notification_service] Email disabled (no RESEND_API_KEY). "
            f"type={type} subject='{subject}'",
            flush=True,
        )
        return False

    # Send email via Resend
    recipient = to_email or NOTIFY_TO_EMAIL
    try:
        import resend  # type: ignore

        resend.api_key = RESEND_API_KEY
        resend.Emails.send(
            {
                "from": FROM_EMAIL,
                "to": recipient,
                "subject": subject,
                "html": html,
            }
        )
        # Mark sent_at on the notification row
        if notif_id:
            sb.table("agent_notifications").update(
                {"sent_at": _now()}
            ).eq("id", notif_id).execute()

        print(
            f"[notification_service] Email sent. type={type} to={recipient}",
            flush=True,
        )
        return True

    except Exception as exc:  # noqa: BLE001
        print(
            f"[notification_service] Failed to send email. "
            f"type={type} error={exc}",
            flush=True,
        )
        return False


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def notify_ideation_complete(sb: Client, brief_id: str, n_ideas: int) -> bool:
    """Notify that ideation has completed and ideas are ready for review."""
    return send_notification(
        sb,
        type="ideation_complete",
        subject=f"Artcaffe AI — {n_ideas} new content ideas ready for review",
        html=(
            f"<p>Hello,</p>"
            f"<p>The ideation agent has generated <strong>{n_ideas} content ideas</strong> "
            f"for brief <code>{brief_id}</code>.</p>"
            f"<p>Please review and approve your favourites in the Artcaffe Marketing dashboard.</p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id, "n_ideas": n_ideas},
    )


def notify_production_complete(sb: Client, brief_id: str) -> bool:
    """Notify that production copy has been generated and is ready."""
    return send_notification(
        sb,
        type="production_complete",
        subject="Artcaffe AI — Final production copy is ready",
        html=(
            f"<p>Hello,</p>"
            f"<p>The production agent has finished writing publication-ready copy "
            f"for brief <code>{brief_id}</code>.</p>"
            f"<p>Please review the final content in the Artcaffe Marketing dashboard.</p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id},
    )


def notify_approval_needed(sb: Client, brief_id: str, title: str) -> bool:
    """Notify that a content item is awaiting approval."""
    return send_notification(
        sb,
        type="approval_needed",
        subject=f"Artcaffe AI — Approval needed: {title}",
        html=(
            f"<p>Hello,</p>"
            f"<p>A content item titled <strong>{title}</strong> is awaiting your approval "
            f"(brief <code>{brief_id}</code>).</p>"
            f"<p>Please approve or reject it in the Artcaffe Marketing dashboard.</p>"
            f"<p>— Artcaffe AI</p>"
        ),
        payload={"brief_id": brief_id, "title": title},
    )

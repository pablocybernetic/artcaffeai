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
    api_key = os.environ.get("RESEND_API_KEY") or RESEND_API_KEY
    from_addr = os.environ.get("NOTIFY_FROM_EMAIL", "noreply@artcaffemarket.co.ke")
    if not api_key:
        print(f"[notification_service] RESEND_API_KEY not set — skipping email to={to_email}", flush=True)
        return False
    try:
        import resend  # type: ignore
        resend.api_key = api_key
        resend.Emails.send({
            "from": from_addr,
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
    brief_id: Optional[str],
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


def _platform_label(platform: str, platform_types: dict) -> str:
    """Return 'Instagram (Reel)' style label."""
    labels = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "linkedin": "LinkedIn",
        "twitter": "X / Twitter",
        "whatsapp": "WhatsApp",
        "google_ads": "Google Ads",
    }
    name = labels.get(platform, platform.replace("_", " ").title())
    ptype = platform_types.get(platform)
    if ptype and ptype != "post":
        name = f"{name} ({ptype.title()})"
    return name


def notify_post_scheduled(
    sb: Client,
    *,
    title: str,
    platforms: list[str],
    publish_at: str,
    platform_types: Optional[dict] = None,
    brief_id: Optional[str] = None,
    content_item_id: Optional[str] = None,
) -> bool:
    """Email confirmation when a post is scheduled for future publishing."""
    pt = platform_types or {}
    platform_lines = "".join(
        f"<li style='margin:4px 0;'>{_platform_label(p, pt)}</li>"
        for p in platforms
    )
    try:
        from datetime import datetime as _dt  # noqa: PLC0415
        dt = _dt.fromisoformat(publish_at.replace("Z", "+00:00"))
        dt_fmt = dt.strftime("%A, %d %B %Y at %H:%M UTC")
    except Exception:
        dt_fmt = publish_at

    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe AI Marketing</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">Hello,</p>
    <p style="font-size:15px;color:#374151;">
      Your post has been <strong>scheduled successfully</strong>.
    </p>
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:16px;margin:16px 0;">
      <p style="margin:0 0 8px;font-weight:700;font-size:14px;color:#1a1a1a;">"{title}"</p>
      <p style="margin:4px 0;font-size:13px;color:#6b7280;">
        <strong>Scheduled for:</strong> {dt_fmt}
      </p>
      <p style="margin:8px 0 4px;font-size:13px;color:#6b7280;"><strong>Platforms:</strong></p>
      <ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;">{platform_lines}</ul>
    </div>
    <a href="{DASHBOARD_URL}/calendar"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      View in Calendar
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
    return send_notification(
        sb,
        type="post_scheduled",
        subject=f'Artcaffe — Scheduled: "{title}"',
        html=html,
        payload={
            "title": title,
            "platforms": platforms,
            "publish_at": publish_at,
            "brief_id": brief_id,
            "content_item_id": content_item_id,
        },
    )


def notify_post_published(
    sb: Client,
    *,
    title: str,
    results: dict,
    platform_types: Optional[dict] = None,
    brief_id: Optional[str] = None,
    content_item_id: Optional[str] = None,
) -> bool:
    """Email confirmation when a post is successfully published to one or more platforms."""
    pt = platform_types or {}
    rows = ""
    for platform, res in results.items():
        ok = res.get("ok", False)
        icon = "✅" if ok else "❌"
        label = _platform_label(platform, pt)
        post_url = res.get("post_url") or res.get("post_id") or ""
        url_cell = (
            f"<a href='{post_url}' style='color:#2563eb;font-size:12px;'>View post</a>"
            if post_url and ok else
            ("<span style='color:#9ca3af;font-size:12px;'>—</span>" if ok else
             f"<span style='color:#ef4444;font-size:12px;'>{str(res.get('error',''))[:80]}</span>")
        )
        rows += (
            f"<tr>"
            f"<td style='padding:8px 12px;font-size:13px;border-bottom:1px solid #f3f4f6;'>"
            f"{icon} {label}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #f3f4f6;'>{url_cell}</td>"
            f"</tr>"
        )

    successful = [p for p, r in results.items() if r.get("ok")]
    subject = (
        f'Artcaffe — Published: "{title}"'
        if successful else
        f'Artcaffe — Publish failed: "{title}"'
    )

    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a;">
  <div style="background:#1a1a1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <p style="color:#fff;font-size:18px;font-weight:700;margin:0;">Artcaffe AI Marketing</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
    <p style="font-size:15px;color:#374151;">Hello,</p>
    <p style="font-size:15px;color:#374151;">
      Your post <strong>"{title}"</strong> has been published.
    </p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;
                  border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">
      <thead>
        <tr style="background:#f9fafb;">
          <th style="padding:10px 12px;font-size:12px;font-weight:600;text-align:left;
                     color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Platform</th>
          <th style="padding:10px 12px;font-size:12px;font-weight:600;text-align:left;
                     color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Result</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <a href="{DASHBOARD_URL}/briefs"
       style="display:inline-block;background:#1a1a1a;color:#fff;padding:10px 20px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
      View in Dashboard
    </a>
    <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— Artcaffe AI Marketing System</p>
  </div>
</div>
"""
    return send_notification(
        sb,
        type="post_published",
        subject=subject,
        html=html,
        payload={
            "title": title,
            "results": {p: {"ok": r.get("ok"), "post_url": r.get("post_url")} for p, r in results.items()},
            "brief_id": brief_id,
            "content_item_id": content_item_id,
        },
    )

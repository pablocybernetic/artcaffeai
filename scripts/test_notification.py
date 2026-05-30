#!/usr/bin/env python3
"""
Quick notification smoke test.
Run on the VM from the app directory:
  cd /opt/artcaffe/app
  python3 /opt/artcaffe/scripts/test_notification.py
"""
import os, sys

# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), "../.env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

# Verify required vars
for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "RESEND_API_KEY"):
    val = os.environ.get(var)
    if not val:
        print(f"MISSING: {var} — check .env")
        sys.exit(1)
    masked = val[:8] + "..." if len(val) > 8 else val
    print(f"  {var} = {masked}")

from_email = os.environ.get("NOTIFY_FROM_EMAIL", "noreply@artcaffemarket.co.ke")
print(f"  FROM_EMAIL = {from_email}")
print()

# Direct Resend test
print("=== Resend direct test ===")
try:
    import resend
    resend.api_key = os.environ["RESEND_API_KEY"]
    result = resend.Emails.send({
        "from": from_email,
        "to": "pgitau@artcaffe.co.ke",
        "subject": "Artcaffe AI — Notification smoke test",
        "html": (
            "<p>This is a test email from the Artcaffe AI notification system.</p>"
            "<p>If you see this, Resend is configured correctly.</p>"
        ),
    })
    print(f"  SUCCESS: {result}")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

print()

# Full notify_approval_needed_to_team test
print("=== notify_approval_needed_to_team test ===")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../app")
    from supabase import create_client
    from notification_service import notify_approval_needed_to_team

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    sent = notify_approval_needed_to_team(sb, brief_id="00000000-0000-0000-0000-000000000000", title="Smoke Test Content Item")
    print(f"  Emails sent: {sent}")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

print()
print("All checks passed.")

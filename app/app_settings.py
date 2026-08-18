"""
app_settings.py
----------------
Generic key/value settings store (table: app_settings) used by the
in-process schedulers (master_scheduler, reminder_scheduler,
meta_sync_scheduler) to persist admin-configured overrides — enabled
flags, intervals, hour windows — across restarts and VM redeploys.
Env vars still supply the very first default on a brand-new deploy.
"""
from __future__ import annotations

from typing import Optional

from supabase import Client


def get_setting(sb: Client, key: str, default: Optional[dict] = None) -> Optional[dict]:
    res = sb.table("app_settings").select("value").eq("key", key).limit(1).execute()
    if res.data:
        return res.data[0]["value"]
    return default


def set_setting(sb: Client, key: str, value: dict) -> None:
    sb.table("app_settings").upsert({"key": key, "value": value}).execute()

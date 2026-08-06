"""
ai_error_log.py
----------------
Backs the frontend's AI-error announcement bar. One row per source (e.g.
"anthropic") holding its most recent failure — cleared the next time that
same provider call succeeds, so the banner disappears on its own once the
underlying issue (e.g. depleted API credits) is resolved.
"""
from __future__ import annotations

from supabase import Client


def record_ai_error(sb: Client, source: str, message: str) -> None:
    try:
        sb.table("ai_error_log").upsert({
            "source": source,
            "message": message[:500],
        }).execute()
    except Exception as e:  # noqa: BLE001 — never let logging break the caller
        print(f"[ai_error_log] failed to record error for '{source}': {e}", flush=True)


def clear_ai_error(sb: Client, source: str) -> None:
    try:
        sb.table("ai_error_log").delete().eq("source", source).execute()
    except Exception as e:  # noqa: BLE001
        print(f"[ai_error_log] failed to clear error for '{source}': {e}", flush=True)

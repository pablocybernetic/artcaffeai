"""
brand_context.py
----------------
Read/write helpers for the `brand_contexts` table.

A brand context is the structured (JSON) version of a concept's brand
guidelines. We keep one active row per concept and bump `version` each time
a new guidelines doc is processed, so prior versions stay auditable.
"""

from __future__ import annotations

from typing import Any, Optional

from supabase import Client


def get_active(sb: Client, concept_id: str) -> Optional[dict[str, Any]]:
    """Return the currently active brand context for a concept, or None."""
    res = (
        sb.table("brand_contexts")
        .select("*")
        .eq("concept_id", concept_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def next_version(sb: Client, concept_id: str) -> int:
    """Return the next version number for a concept (1 if none exist)."""
    res = (
        sb.table("brand_contexts")
        .select("version")
        .eq("concept_id", concept_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data[0]["version"] + 1) if res.data else 1


def deactivate_all(sb: Client, concept_id: str) -> None:
    """Mark every existing row for a concept as inactive."""
    sb.table("brand_contexts").update({"is_active": False}).eq(
        "concept_id", concept_id
    ).eq("is_active", True).execute()


def insert_active(
    sb: Client,
    *,
    concept_id: str,
    version: int,
    content: dict[str, Any],
    source_file_path: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a new active brand context row and return it."""
    res = (
        sb.table("brand_contexts")
        .insert(
            {
                "concept_id": concept_id,
                "version": version,
                "is_active": True,
                "source_file_path": source_file_path,
                "context_json": content,
            }
        )
        .execute()
    )
    return res.data[0]


def replace_active(
    sb: Client,
    *,
    concept_id: str,
    content: dict[str, Any],
    source_file_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Convenience: deactivate previous active row, insert a new one with
    version = previous + 1. Returns the new row.
    """
    v = next_version(sb, concept_id)
    deactivate_all(sb, concept_id)
    return insert_active(
        sb,
        concept_id=concept_id,
        version=v,
        content=content,
        source_file_path=source_file_path,
    )

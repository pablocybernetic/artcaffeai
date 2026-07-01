"""
brand_assets_routes.py
----------------------
Upload and list brand asset files stored in Supabase Storage.
Tags + descriptions are persisted in the `brand_assets` DB table.

Three asset categories, all in the 'brand-assets' bucket under subfolders:
  logos/              — PNG, JPG, SVG, WEBP, PDF
  image-guidelines/   — PDF, DOCX
  video-guidelines/   — PDF, DOCX, MP4, MOV

Routes:
  GET   /brand-assets/{category}                    — list files (with tags)
  POST  /brand-assets/{category}/upload             — upload file
  PATCH /brand-assets/{category}/{filename}/tags    — update tags + description
  DEL   /brand-assets/{category}/{filename}         — delete file

Supabase migration (run once in the SQL editor):
  CREATE TABLE IF NOT EXISTS brand_assets (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    category text NOT NULL,
    filename text NOT NULL,
    tags text[] NOT NULL DEFAULT '{}',
    description text NOT NULL DEFAULT '',
    updated_at timestamptz DEFAULT now(),
    UNIQUE(category, filename)
  );
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File
from pydantic import BaseModel

from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
BRAND_ASSETS_BUCKET = os.environ.get("BRAND_ASSETS_BUCKET", "brand-assets")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/brand-assets", tags=["brand-assets"])

CATEGORIES = {"logos", "image-guidelines", "video-guidelines"}

MIME_MAP: dict[str, str] = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "svg":  "image/svg+xml",
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc":  "application/msword",
    "mp4":  "video/mp4",
    "mov":  "video/quicktime",
}

ALLOWED_BY_CATEGORY: dict[str, set[str]] = {
    "logos":             {"png", "jpg", "jpeg", "webp", "svg", "pdf"},
    "image-guidelines":  {"pdf", "docx", "doc"},
    "video-guidelines":  {"pdf", "docx", "doc", "mp4", "mov"},
}


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _validate_category(category: str) -> None:
    if category not in CATEGORIES:
        raise HTTPException(400, f"Unknown category '{category}'. Use: {', '.join(sorted(CATEGORIES))}")


def _storage_path(category: str, filename: str) -> str:
    return f"{category}/{filename}"


def _fetch_meta(category: str) -> dict[str, dict]:
    """
    Fetch tags + descriptions from brand_assets table for all files in a category.
    Returns {filename: {tags, description}}. Silently returns {} if table missing.
    """
    try:
        res = (
            sb.table("brand_assets")
            .select("filename,tags,description")
            .eq("category", category)
            .execute()
        )
        return {
            row["filename"]: {
                "tags": row.get("tags") or [],
                "description": row.get("description") or "",
            }
            for row in (res.data or [])
        }
    except Exception as e:
        print(f"[brand_assets] meta fetch skipped ({e})", flush=True)
        return {}


def _upsert_meta(category: str, filename: str, tags: list[str], description: str = "") -> None:
    """Upsert tags + description for a file. Silently ignores if table missing."""
    try:
        sb.table("brand_assets").upsert(
            {
                "category":    category,
                "filename":    filename,
                "tags":        tags,
                "description": description,
                "updated_at":  "now()",
            },
            on_conflict="category,filename",
        ).execute()
    except Exception as e:
        print(f"[brand_assets] meta upsert skipped ({e})", flush=True)


def _delete_meta(category: str, filename: str) -> None:
    """Remove a file's metadata row. Silently ignores if table missing."""
    try:
        sb.table("brand_assets").delete().eq("category", category).eq("filename", filename).execute()
    except Exception as e:
        print(f"[brand_assets] meta delete skipped ({e})", flush=True)


# ---------------------------------------------------------------------------
# Pydantic body models
# ---------------------------------------------------------------------------

class TagsUpdateBody(BaseModel):
    tags: list[str] = []
    description: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{category}")
def list_assets(category: str, _: None = Depends(require_api_key)):
    """List all files in a category, merged with their tags and descriptions."""
    _validate_category(category)
    try:
        storage_res = sb.storage.from_(BRAND_ASSETS_BUCKET).list(
            category,
            {"limit": 200, "sortBy": {"column": "name", "order": "asc"}},
        )
        meta = _fetch_meta(category)
        files = [
            {
                "name":        f["name"],
                "size":        (f.get("metadata") or {}).get("size", 0),
                "url":         sb.storage.from_(BRAND_ASSETS_BUCKET).get_public_url(
                                   _storage_path(category, f["name"])
                               ),
                "tags":        meta.get(f["name"], {}).get("tags", []),
                "description": meta.get(f["name"], {}).get("description", ""),
            }
            for f in (storage_res or [])
            if f.get("name") and f["name"] != ".emptyFolderPlaceholder"
        ]
        return {"ok": True, "category": category, "files": files}
    except Exception as e:
        raise HTTPException(500, f"Could not list {category}: {e}")


@router.post("/{category}/upload")
async def upload_asset(
    category: str,
    file: UploadFile = File(...),
    _: None = Depends(require_api_key),
):
    """Upload a brand asset file and create an empty metadata record."""
    _validate_category(category)

    name = (file.filename or "file").replace(" ", "_")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    allowed = ALLOWED_BY_CATEGORY[category]
    if ext not in allowed:
        raise HTTPException(
            400,
            f"Unsupported file type .{ext} for {category}. Allowed: {', '.join(sorted(allowed))}",
        )

    content = await file.read()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    path = _storage_path(category, name)

    try:
        sb.storage.from_(BRAND_ASSETS_BUCKET).upload(
            path,
            content,
            file_options={"content-type": mime, "upsert": "true"},
        )
        url = sb.storage.from_(BRAND_ASSETS_BUCKET).get_public_url(path)
        # Create metadata row (empty tags — user adds them after upload)
        _upsert_meta(category, name, [])
        return {"ok": True, "name": name, "size": len(content), "url": url}
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")


@router.patch("/{category}/{filename}/tags")
def update_tags(
    category: str,
    filename: str,
    body: TagsUpdateBody,
    _: None = Depends(require_api_key),
):
    """Update tags and description for an uploaded file."""
    _validate_category(category)
    # Sanitise tags: strip, lowercase, deduplicate, drop empties
    clean_tags = list(dict.fromkeys(
        t.strip().lower() for t in body.tags if t.strip()
    ))
    _upsert_meta(category, filename, clean_tags, body.description.strip())
    return {"ok": True, "filename": filename, "tags": clean_tags}


@router.delete("/{category}/{filename}")
def delete_asset(
    category: str,
    filename: str,
    _: None = Depends(require_api_key),
):
    """Delete a brand asset file and its metadata."""
    _validate_category(category)
    path = _storage_path(category, filename)
    try:
        sb.storage.from_(BRAND_ASSETS_BUCKET).remove([path])
        _delete_meta(category, filename)
        return {"ok": True, "deleted": filename}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")

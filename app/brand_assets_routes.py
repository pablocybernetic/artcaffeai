"""
brand_assets_routes.py
----------------------
Upload and list brand asset files stored in Supabase Storage.

Three asset categories, all in the 'brand-assets' bucket under subfolders:
  logos/              — PNG, JPG, SVG, WEBP, PDF
  image-guidelines/   — PDF, DOCX
  video-guidelines/   — PDF, DOCX, MP4, MOV

Routes:
  GET  /brand-assets/{category}                — list files
  POST /brand-assets/{category}/upload         — upload file
  DEL  /brand-assets/{category}/{filename}     — delete file
"""
from __future__ import annotations

import os
from typing import Optional, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File

from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
BRAND_ASSETS_BUCKET = os.environ.get("BRAND_ASSETS_BUCKET", "brand-assets")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/brand-assets", tags=["brand-assets"])

CATEGORIES = {"logos", "image-guidelines", "video-guidelines"}

MIME_MAP: dict[str, str] = {
    # logos
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "svg":  "image/svg+xml",
    # documents
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc":  "application/msword",
    # video
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


@router.get("/{category}")
def list_assets(category: str, _: None = Depends(require_api_key)):
    """List all files in a category."""
    _validate_category(category)
    try:
        res = sb.storage.from_(BRAND_ASSETS_BUCKET).list(
            category,
            {"limit": 200, "sortBy": {"column": "name", "order": "asc"}},
        )
        files = [
            {
                "name": f["name"],
                "size": (f.get("metadata") or {}).get("size", 0),
                "url": sb.storage.from_(BRAND_ASSETS_BUCKET).get_public_url(
                    _storage_path(category, f["name"])
                ),
            }
            for f in (res or [])
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
    """Upload a brand asset file to the specified category subfolder."""
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
        return {"ok": True, "name": name, "size": len(content), "url": url}
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")


@router.delete("/{category}/{filename}")
def delete_asset(
    category: str,
    filename: str,
    _: None = Depends(require_api_key),
):
    """Delete a brand asset file."""
    _validate_category(category)
    path = _storage_path(category, filename)
    try:
        sb.storage.from_(BRAND_ASSETS_BUCKET).remove([path])
        return {"ok": True, "deleted": filename}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")

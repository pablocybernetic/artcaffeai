"""
font_routes.py
--------------
Upload and list brand font files stored in Supabase Storage.
Uses the service-role key so RLS is bypassed — these are internal admin operations.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
FONTS_BUCKET = os.environ.get("FONTS_BUCKET", "fonts")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/fonts", tags=["fonts"])


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


MIME_MAP = {
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
}


@router.get("")
def list_fonts(_: None = Depends(require_api_key)):
    """List all font files in the fonts bucket."""
    try:
        res = sb.storage.from_(FONTS_BUCKET).list("", {"limit": 200, "sortBy": {"column": "name", "order": "asc"}})
        files = [
            {
                "name": f["name"],
                "size": (f.get("metadata") or {}).get("size", 0),
            }
            for f in (res or [])
            if f.get("name") and f["name"] != ".emptyFolderPlaceholder"
        ]
        return {"ok": True, "fonts": files}
    except Exception as e:
        raise HTTPException(500, f"Could not list fonts: {e}")


@router.post("/upload")
async def upload_font(
    file: UploadFile = File(...),
    _: None = Depends(require_api_key),
):
    """Upload a font file to Supabase Storage."""
    name = file.filename or "font"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in MIME_MAP:
        raise HTTPException(400, f"Unsupported font type: .{ext}. Use TTF, OTF, WOFF, or WOFF2.")

    content = await file.read()
    mime = MIME_MAP[ext]

    try:
        sb.storage.from_(FONTS_BUCKET).upload(
            name,
            content,
            file_options={"content-type": mime, "upsert": "true"},
        )
        return {"ok": True, "name": name, "size": len(content)}
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")


@router.delete("/{filename}")
def delete_font(filename: str, _: None = Depends(require_api_key)):
    """Delete a font file from Supabase Storage."""
    try:
        sb.storage.from_(FONTS_BUCKET).remove([filename])
        return {"ok": True, "deleted": filename}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")

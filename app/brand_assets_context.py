"""
brand_assets_context.py
-----------------------
Loads brand asset metadata (logos, image guidelines, video guidelines) from the
brand-assets Supabase bucket and formats it for injection into agent prompts.

PDF guidelines are extracted once using Claude (Haiku vision) and the result is
cached in the brand_assets.description column — all subsequent calls read from
the DB with zero extra API cost.

An in-process TTL cache (5 min) avoids a DB round-trip on every generation.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any

from supabase import Client

BRAND_ASSETS_BUCKET = os.environ.get("BRAND_ASSETS_BUCKET", "brand-assets")

_CACHE: dict = {}
_CACHE_TTL = 300  # seconds


def _extract_pdf_rules(
    sb: Client,
    anthropic: Any,
    category: str,
    filename: str,
) -> str:
    """
    Download a guidelines PDF from Supabase Storage and ask Claude to extract
    the key creative rules as a bullet-point summary.
    Returns the summary string, or "" on any failure.
    Skips files larger than 8 MB.
    """
    try:
        data: bytes = sb.storage.from_(BRAND_ASSETS_BUCKET).download(
            f"{category}/{filename}"
        )
    except Exception as exc:
        print(f"[brand_assets_context] download failed for {filename}: {exc}", flush=True)
        return ""

    if len(data) > 8 * 1024 * 1024:
        print(f"[brand_assets_context] {filename} is {len(data)//1024} KB — skipping extraction", flush=True)
        return ""

    try:
        b64 = base64.standard_b64encode(data).decode()
        resp = anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract the key design and creative rules from this brand guidelines document. "
                            "Return a concise bullet-point list of the most important rules an AI art director "
                            "must follow. Focus on: prohibited elements, required visual elements, colour usage, "
                            "composition rules, typography rules, overlay and text rules. "
                            "Be specific and actionable. Under 250 words. Start each point with '- '."
                        ),
                    },
                ],
            }],
            timeout=45.0,
        )
        rules = resp.content[0].text.strip()
        print(f"[brand_assets_context] extracted {len(rules)} chars from {filename}", flush=True)
        return rules
    except Exception as exc:
        print(f"[brand_assets_context] PDF extraction failed for {filename}: {exc}", flush=True)
        return ""


def load_brand_assets_context(
    sb: Client,
    anthropic: Any = None,
    *,
    force_refresh: bool = False,
) -> dict:
    """
    Load brand assets metadata from Supabase.

    Returns:
        {
          "logos": [{"name", "url", "tags", "description"}],
          "image_guidelines": {"files": [...], "rules": "<extracted text>"},
          "video_guidelines": {"files": [...], "rules": "<extracted text>"},
        }

    PDF rules are extracted on first call per file and cached in brand_assets.description.
    Subsequent calls read the cached description — no extra Claude API call.
    """
    now = time.monotonic()
    if (
        not force_refresh
        and _CACHE.get("data")
        and now - _CACHE.get("ts", 0) < _CACHE_TTL
    ):
        return _CACHE["data"]

    result: dict = {
        "logos": [],
        "image_guidelines": {"files": [], "rules": ""},
        "video_guidelines": {"files": [], "rules": ""},
    }

    # category DB key → result dict key
    cat_map = {
        "logos":             "logos",
        "image-guidelines":  "image_guidelines",
        "video-guidelines":  "video_guidelines",
    }

    for cat_key, result_key in cat_map.items():
        try:
            rows = (
                sb.table("brand_assets")
                .select("filename,tags,description")
                .eq("category", cat_key)
                .execute()
                .data or []
            )
        except Exception as exc:
            print(f"[brand_assets_context] DB read skipped for {cat_key}: {exc}", flush=True)
            continue

        for row in rows:
            filename = row.get("filename", "")
            if not filename:
                continue

            try:
                url = sb.storage.from_(BRAND_ASSETS_BUCKET).get_public_url(
                    f"{cat_key}/{filename}"
                )
            except Exception:
                url = ""

            file_info = {
                "name":        filename,
                "url":         url,
                "tags":        row.get("tags") or [],
                "description": row.get("description") or "",
            }

            if result_key == "logos":
                result["logos"].append(file_info)
                continue

            result[result_key]["files"].append(file_info)

            if row.get("description"):
                # Already extracted — just append (there may be multiple guideline files)
                existing = result[result_key]["rules"]
                sep = "\n" if existing else ""
                result[result_key]["rules"] = existing + sep + row["description"]

            elif anthropic and filename.lower().endswith(".pdf"):
                # First encounter — extract rules and write back to description cache
                rules = _extract_pdf_rules(sb, anthropic, cat_key, filename)
                if rules:
                    result[result_key]["rules"] = rules
                    try:
                        sb.table("brand_assets").update(
                            {"description": rules}
                        ).eq("category", cat_key).eq("filename", filename).execute()
                        print(
                            f"[brand_assets_context] cached rules → brand_assets.description for {filename}",
                            flush=True,
                        )
                    except Exception as exc:
                        print(f"[brand_assets_context] cache write skipped: {exc}", flush=True)

    _CACHE["data"] = result
    _CACHE["ts"] = now
    return result


# ── Formatters — one per agent type ──────────────────────────────────────────

def format_for_ideation(ctx: dict) -> str:
    """Full context block for the ideation agent — logos + both guideline sets."""
    lines: list[str] = []

    if ctx.get("logos"):
        names = ", ".join(f["name"] for f in ctx["logos"])
        lines.append(f"BRAND LOGOS ON FILE: {names}")
        tagged = [
            f"  {f['name']}: [{', '.join(f['tags'])}]"
            for f in ctx["logos"] if f.get("tags")
        ]
        lines.extend(tagged)

    img_rules = ctx.get("image_guidelines", {}).get("rules", "")
    if img_rules:
        lines += ["", "IMAGE GUIDELINES (inform every visual idea):"]
        lines.append(img_rules)

    vid_rules = ctx.get("video_guidelines", {}).get("rules", "")
    if vid_rules:
        lines += ["", "VIDEO GUIDELINES (inform every video idea):"]
        lines.append(vid_rules)

    return "\n".join(lines).strip()


def format_for_image_prompt(ctx: dict) -> str:
    """Context block for image generation — logos + image guideline rules only."""
    lines: list[str] = []

    if ctx.get("logos"):
        names = ", ".join(f["name"] for f in ctx["logos"])
        lines.append(f"BRAND LOGOS ON FILE: {names}")

    rules = ctx.get("image_guidelines", {}).get("rules", "")
    if rules:
        lines += ["", "BRAND IMAGE GUIDELINES (official document — MUST FOLLOW):"]
        lines.append(rules)

    return "\n".join(lines).strip()


def format_for_video_prompt(ctx: dict) -> str:
    """Context block for video generation — video guideline rules only."""
    rules = ctx.get("video_guidelines", {}).get("rules", "")
    if not rules:
        return ""
    return "BRAND VIDEO GUIDELINES (official document — MUST FOLLOW):\n" + rules

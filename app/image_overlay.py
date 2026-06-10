"""
image_overlay.py
-----------------
Burns headline text onto a banner image using the brand fonts stored in
Supabase Storage.  Called by image_agent after the raw image is generated
and before it is uploaded.

Design: bottom-third semi-transparent scrim + white ALL-CAPS headline text
centred horizontally, using the first available Gotham variant (or any font
found in the bucket, falling back to Pillow's built-in font).
"""
from __future__ import annotations

import io
import os
from typing import Optional

from supabase import Client

FONTS_BUCKET = os.environ.get("FONTS_BUCKET", "fonts")

# Preferred font order for headlines
_FONT_PREFERENCE = [
    "Gotham-Medium.otf",
    "Gotham-Book.otf",
    "Gotham-Light.otf",
    "GaramondITCbyBT-Bold.otf",
    "Paris.otf",
    "Lovelo_Line_Light.otf",
]


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

def _fetch_best_font_bytes(sb: Client) -> Optional[bytes]:
    """Download the best available brand font from storage. Returns None on failure."""
    candidates = list(_FONT_PREFERENCE)
    try:
        listing = sb.storage.from_(FONTS_BUCKET).list("", {"limit": 100})
        for f in listing or []:
            name = f.get("name") or ""
            if name and name not in candidates and name != ".emptyFolderPlaceholder":
                candidates.append(name)
    except Exception:
        pass

    for name in candidates:
        try:
            data = sb.storage.from_(FONTS_BUCKET).download(name)
            if data:
                print(f"[image_overlay] using font: {name}", flush=True)
                return data
        except Exception:
            continue
    return None


def _pil_font(font_bytes: Optional[bytes], size: int):
    """Create a PIL ImageFont at the given size. Falls back to default."""
    from PIL import ImageFont  # local import — Pillow may not be installed on all envs

    if font_bytes:
        try:
            return ImageFont.truetype(io.BytesIO(font_bytes), size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Text wrapping
# ---------------------------------------------------------------------------

def _wrap(text: str, font, max_px: int, draw) -> list[str]:
    """Split text into lines that fit within max_px."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


# ---------------------------------------------------------------------------
# Main overlay function
# ---------------------------------------------------------------------------

def overlay_headline(
    image_bytes: bytes,
    headline: str,
    sb: Client,
) -> bytes:
    """
    Overlay the headline on the image as white text over a dark scrim.
    Returns composited PNG bytes.  On any error returns the original bytes.
    """
    try:
        from PIL import Image, ImageDraw  # local import
    except ImportError:
        print("[image_overlay] Pillow not installed — skipping text overlay", flush=True)
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        W, H = img.size

        font_bytes = _fetch_best_font_bytes(sb)

        # Headline font size: ~5.5% of width, min 36px, max 90px
        headline_size = max(36, min(90, int(W * 0.055)))
        h_font = _pil_font(font_bytes, headline_size)

        padding_x = int(W * 0.06)
        padding_y = int(H * 0.04)
        max_text_w = W - padding_x * 2

        # Scratch draw for measuring
        scratch = ImageDraw.Draw(img.copy())

        text = headline.upper()
        lines = _wrap(text, h_font, max_text_w, scratch)
        # Cap at 3 lines
        if len(lines) > 3:
            lines = lines[:3]
            lines[-1] = lines[-1].rstrip() + "…"

        def line_height(font) -> int:
            bb = scratch.textbbox((0, 0), "Ag", font=font)
            return bb[3] - bb[1]

        lh = line_height(h_font)
        line_gap = int(lh * 0.25)

        total_text_h = lh * len(lines) + line_gap * (len(lines) - 1)
        scrim_h = total_text_h + padding_y * 2
        scrim_top = H - scrim_h - int(H * 0.025)

        # ── Draw overlay ────────────────────────────────────────────
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Semi-transparent dark scrim
        draw.rectangle(
            [(0, scrim_top), (W, H - int(H * 0.018))],
            fill=(8, 8, 8, 178),  # ~70 % opacity
        )

        # White headline text — one thin bright line above the scrim for polish
        draw.rectangle(
            [(int(W * 0.08), scrim_top), (int(W * 0.92), scrim_top + 2)],
            fill=(255, 255, 255, 120),
        )

        y = scrim_top + padding_y
        for line in lines:
            bb = draw.textbbox((0, 0), line, font=h_font)
            lw = bb[2] - bb[0]
            x = (W - lw) // 2
            # Soft shadow for depth
            draw.text((x + 2, y + 2), line, font=h_font, fill=(0, 0, 0, 140))
            # Main text
            draw.text((x, y), line, font=h_font, fill=(255, 255, 255, 255))
            y += lh + line_gap

        result = Image.alpha_composite(img, overlay).convert("RGB")
        buf = io.BytesIO()
        result.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(f"[image_overlay] overlay applied — {len(data) // 1024} KB", flush=True)
        return data

    except Exception as exc:
        print(f"[image_overlay] overlay failed, returning original: {exc}", flush=True)
        return image_bytes

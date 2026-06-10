"""
image_overlay.py
-----------------
Burns headline text onto a banner image using the brand fonts stored in
Supabase Storage.  Called by image_agent after the raw image is generated
and before it is uploaded.

Claude vision (Haiku) analyses the image and picks the best placement
(top / center / bottom) based on where there is the most open, dark, or
uncluttered space.  Falls back to bottom if the vision call fails.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Optional

from supabase import Client

FONTS_BUCKET = os.environ.get("FONTS_BUCKET", "fonts")
_PLACEMENT_MODEL = "claude-haiku-4-5-20251001"

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
    from PIL import ImageFont
    if font_bytes:
        try:
            return ImageFont.truetype(io.BytesIO(font_bytes), size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Claude vision — pick best placement
# ---------------------------------------------------------------------------

def _pick_placement(image_bytes: bytes, anthropic: Any) -> str:
    """
    Send a small version of the image to Claude Haiku and ask where to place
    the headline text.  Returns 'top', 'center', or 'bottom'.
    """
    try:
        from PIL import Image  # local import

        # Resize to 512px wide for a cheap vision call
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()

        resp = anthropic.messages.create(
            model=_PLACEMENT_MODEL,
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "For this marketing photo, where is the darkest, simplest, or most "
                            "open area to place a white headline? Avoid areas with existing text, "
                            "logos, or busy detail. Reply with exactly one word: "
                            "top, upper-third, center, lower-third, or bottom."
                        ),
                    },
                ],
            }],
        )
        answer = resp.content[0].text.strip().lower()
        if "upper" in answer:
            placement = "upper-third"
        elif "lower" in answer:
            placement = "lower-third"
        elif "top" in answer:
            placement = "top"
        elif "center" in answer or "middle" in answer:
            placement = "center"
        else:
            placement = "bottom"
        print(f"[image_overlay] Claude chose placement: {placement}", flush=True)
        return placement
    except Exception as exc:
        print(f"[image_overlay] placement vision call failed, using bottom: {exc}", flush=True)
        return "bottom"


# ---------------------------------------------------------------------------
# Text wrapping
# ---------------------------------------------------------------------------

def _wrap(text: str, font, max_px: int, draw) -> list[str]:
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
    *,
    anthropic: Any = None,
) -> bytes:
    """
    Overlay the headline on the image as white text over a dark scrim.
    If anthropic client is provided, Claude picks the best placement.
    Returns composited PNG bytes.  On any error returns the original bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[image_overlay] Pillow not installed — skipping text overlay", flush=True)
        return image_bytes

    try:
        # Ask Claude where to place the text
        placement = _pick_placement(image_bytes, anthropic) if anthropic else "bottom"

        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        W, H = img.size

        font_bytes = _fetch_best_font_bytes(sb)
        headline_size = max(36, min(90, int(W * 0.055)))
        h_font = _pil_font(font_bytes, headline_size)

        padding_x = int(W * 0.06)
        padding_y = int(H * 0.038)
        max_text_w = W - padding_x * 2

        scratch = ImageDraw.Draw(img.copy())
        text = headline.upper()
        lines = _wrap(text, h_font, max_text_w, scratch)
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
        margin = int(H * 0.022)

        # Text anchor Y based on Claude's placement choice
        if placement == "top":
            text_y = margin + padding_y
        elif placement == "upper-third":
            text_y = int(H * 0.18)
        elif placement == "center":
            text_y = (H - total_text_h) // 2
        elif placement == "lower-third":
            text_y = int(H * 0.62)
        else:  # bottom
            text_y = H - total_text_h - padding_y - margin

        # ── Draw text with outline-shadow (no background scrim) ─────
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Shadow offsets: render black text in a ring around the letter
        # for a solid outline effect readable on any background
        shadow_offsets = [
            (dx, dy)
            for dx in range(-3, 4)
            for dy in range(-3, 4)
            if abs(dx) + abs(dy) <= 4 and (dx or dy)
        ]

        y = text_y
        for line in lines:
            bb = draw.textbbox((0, 0), line, font=h_font)
            lw = bb[2] - bb[0]
            x = (W - lw) // 2
            # Outline/shadow pass
            for dx, dy in shadow_offsets:
                draw.text((x + dx, y + dy), line, font=h_font, fill=(0, 0, 0, 200))
            # Main white text
            draw.text((x, y), line, font=h_font, fill=(255, 255, 255, 255))
            y += lh + line_gap

        result = Image.alpha_composite(img, overlay).convert("RGB")
        buf = io.BytesIO()
        result.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(f"[image_overlay] overlay applied ({placement}) — {len(data) // 1024} KB", flush=True)
        return data

    except Exception as exc:
        print(f"[image_overlay] overlay failed, returning original: {exc}", flush=True)
        return image_bytes

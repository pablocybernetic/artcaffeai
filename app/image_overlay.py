"""
image_overlay.py
----------------
Composites branded text onto a marketing image using one of four layout templates.

Templates (Claude picks automatically):
  hero   — full-bleed image, gradient scrim, headline + body text over it
  band   — image fills top 60 %, solid brand-colour band fills bottom 40 %
  split  — image fills right 55 %, brand-colour panel fills left 45 %
  solid  — solid brand colour (no photo), strong typographic card

Each template renders:
  • Headline  — large, uppercase, bold weight font
  • Body copy — smaller, regular weight font (lighter variant if available)
  • Optional CTA line — small caps

Fonts are loaded from the Supabase `fonts` bucket.
Brand colour is passed in from brand_contexts; falls back to Artcaffe forest green.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Any, Optional

from supabase import Client

FONTS_BUCKET    = os.environ.get("FONTS_BUCKET", "fonts")
_LAYOUT_MODEL   = "claude-haiku-4-5-20251001"
_DEFAULT_COLOR  = "#1B3A2A"   # Artcaffe forest green

# ── Font preference lists ────────────────────────────────────────────────────
_BOLD_PREF = [
    "Gotham-Medium.otf", "Gotham-Bold.otf", "Gotham-Black.otf",
    "GaramondITCbyBT-Bold.otf", "Paris.otf",
]
_LIGHT_PREF = [
    "Gotham-Book.otf", "Gotham-Light.otf", "Lovelo_Line_Light.otf",
    "GaramondITCbyBT-Book.otf",
]


# ── Font loading ─────────────────────────────────────────────────────────────

def _fetch_fonts(sb: Client) -> dict[str, Optional[bytes]]:
    """Return {'bold': bytes|None, 'light': bytes|None} from Supabase fonts bucket."""
    try:
        listing = sb.storage.from_(FONTS_BUCKET).list("", {"limit": 100})
        available = [
            f.get("name", "") for f in (listing or [])
            if f.get("name") and f["name"] != ".emptyFolderPlaceholder"
        ]
    except Exception:
        available = []

    def _load_first(candidates: list[str]) -> Optional[bytes]:
        for name in candidates:
            try:
                data = sb.storage.from_(FONTS_BUCKET).download(name)
                if data:
                    print(f"[image_overlay] loaded font: {name}", flush=True)
                    return data
            except Exception:
                continue
        return None

    bold_pref  = _BOLD_PREF  + [n for n in available if any(k in n.lower() for k in ("bold","medium","heavy","black"))]
    light_pref = _LIGHT_PREF + [n for n in available if any(k in n.lower() for k in ("light","book","regular","thin"))]
    fallback   = list(dict.fromkeys(bold_pref + light_pref + available))

    bold  = _load_first(bold_pref)  or _load_first(fallback)
    light = _load_first(light_pref) or bold   # same font if no lighter variant

    return {"bold": bold, "light": light}


def _pil_font(font_bytes: Optional[bytes], size: int):
    from PIL import ImageFont
    if font_bytes:
        try:
            return ImageFont.truetype(io.BytesIO(font_bytes), size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Colour utilities ─────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _text_color(bg_hex: str) -> tuple[int, int, int]:
    """Return white or near-black depending on background luminance."""
    r, g, b = _hex_to_rgb(bg_hex)
    return (240, 235, 228) if _luminance(r, g, b) < 140 else (18, 16, 14)


# ── Claude layout picker ─────────────────────────────────────────────────────

def _pick_layout(image_bytes: bytes, anthropic: Any, platform: str) -> dict:
    """
    Ask Claude Haiku to choose template + hero text placement.
    Returns {"template": str, "placement": str}.
    Falls back to {"template": "band", "placement": "bottom"}.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()

        resp = anthropic.messages.create(
            model=_LAYOUT_MODEL,
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": (
                        f"This image is for a {platform} marketing creative. "
                        "Choose the best layout template:\n"
                        "  hero  — full image, gradient scrim, text over photo\n"
                        "  band  — image top 60%, colour band bottom with text\n"
                        "  split — image right 55%, colour panel left with text\n"
                        "  solid — solid colour background only, typographic\n\n"
                        "Also choose hero placement (only used for hero template): top / center / bottom\n\n"
                        "Reply ONLY with JSON: {\"template\":\"band\",\"placement\":\"bottom\"}"
                    )},
                ],
            }],
            timeout=20.0,
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{[^}]+\}', raw)
        result = json.loads(m.group()) if m else {}
        template  = result.get("template", "band")
        placement = result.get("placement", "bottom")
        if template  not in ("hero", "band", "split", "solid"): template  = "band"
        if placement not in ("top", "upper-third", "center", "lower-third", "bottom"): placement = "bottom"
        print(f"[image_overlay] layout={template} placement={placement}", flush=True)
        return {"template": template, "placement": placement}
    except Exception as exc:
        print(f"[image_overlay] layout pick failed ({exc}), using band/bottom", flush=True)
        return {"template": "band", "placement": "bottom"}


# ── Text utilities ───────────────────────────────────────────────────────────

def _wrap(text: str, font, max_px: int, draw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        w = draw.textbbox((0, 0), candidate, font=font)[2]
        if w <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _line_h(font, draw) -> int:
    bb = draw.textbbox((0, 0), "Ag", font=font)
    return bb[3] - bb[1]


def _draw_text_block(
    draw,
    headline: str,
    body: str,
    h_font,
    b_font,
    max_w: int,
    x_center: int,
    y_top: int,
    text_rgb: tuple[int, int, int],
    shadow: bool = False,
) -> int:
    """
    Render headline then body copy.  Returns the y coordinate below the last line.
    x_center is the horizontal midpoint of the text zone.
    """
    gap = max(8, int(_line_h(h_font, draw) * 0.2))

    def _draw_line(line: str, font, y: int) -> int:
        bb    = draw.textbbox((0, 0), line, font=font)
        lw    = bb[2] - bb[0]
        x     = x_center - lw // 2
        lh    = bb[3] - bb[1]
        if shadow:
            offsets = [(dx, dy) for dx in range(-3,4) for dy in range(-3,4)
                       if abs(dx)+abs(dy) <= 4 and (dx or dy)]
            for dx, dy in offsets:
                draw.text((x+dx, y+dy), line, font=font, fill=(0,0,0,180))
        draw.text((x, y), line, font=font, fill=(*text_rgb, 255))
        return y + lh + gap

    # Headline (uppercase)
    h_lines = _wrap(headline.upper(), h_font, max_w, draw)
    if len(h_lines) > 3:
        h_lines = h_lines[:3]

    y = y_top
    for line in h_lines:
        y = _draw_line(line, h_font, y)

    # Spacer between headline and body
    if body:
        y += gap

    # Body copy
    if body:
        b_lines = _wrap(body, b_font, max_w, draw)
        if len(b_lines) > 4:
            b_lines = b_lines[:4]
            b_lines[-1] = b_lines[-1].rstrip() + "…"
        for line in b_lines:
            y = _draw_line(line, b_font, y)

    return y


# ── Template renderers ───────────────────────────────────────────────────────

def _render_hero(
    img,
    headline: str,
    body: str,
    h_font,
    b_font,
    brand_color: str,
    placement: str,
) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageFilter

    W, H = img.size
    canvas = img.convert("RGBA").copy()

    pad_x  = int(W * 0.07)
    pad_y  = int(H * 0.05)
    max_tw = W - pad_x * 2

    # Measure text block height
    tmp_draw = ImageDraw.Draw(Image.new("RGBA", (W, H)))
    h_lh = _line_h(h_font, tmp_draw)
    b_lh = _line_h(b_font, tmp_draw)
    h_lines = _wrap(headline.upper(), h_font, max_tw, tmp_draw)[:3]
    b_lines = (_wrap(body, b_font, max_tw, tmp_draw)[:4] if body else [])
    gap = max(8, int(h_lh * 0.2))
    block_h = (
        h_lh * len(h_lines) + gap * len(h_lines)
        + (b_lh * len(b_lines) + gap * len(b_lines) + gap if b_lines else 0)
    )

    # Y position of the block
    if placement in ("top", "upper-third"):
        text_y = pad_y if placement == "top" else int(H * 0.15)
    elif placement == "center":
        text_y = (H - block_h) // 2
    elif placement == "lower-third":
        text_y = int(H * 0.58)
    else:  # bottom
        text_y = H - block_h - pad_y

    # Gradient scrim — dark at text zone, transparent above
    scrim_top    = max(0, text_y - pad_y * 2)
    scrim_height = H - scrim_top
    scrim = Image.new("RGBA", (W, scrim_height), (0, 0, 0, 0))
    for row in range(scrim_height):
        alpha = int(210 * (row / scrim_height) ** 0.6)
        for col in range(W):
            scrim.putpixel((col, row), (0, 0, 0, alpha))

    canvas.paste(scrim, (0, scrim_top), scrim)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    _draw_text_block(draw, headline, body, h_font, b_font, max_tw, W//2, text_y, (240,235,228), shadow=False)
    return Image.alpha_composite(canvas, overlay)


def _render_band(img, headline: str, body: str, h_font, b_font, brand_color: str) -> "Image.Image":
    from PIL import Image, ImageDraw

    W, H   = img.size
    img_h  = int(H * 0.60)
    band_h = H - img_h

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # Image fills top
    photo = _cover_crop(img.convert("RGBA"), W, img_h)
    canvas.paste(photo, (0, 0))

    # Brand-colour band at bottom
    r, g, b = _hex_to_rgb(brand_color)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, img_h), (W, H)], fill=(r, g, b, 255))

    # Text centered in band
    text_rgb = _text_color(brand_color)
    pad_x    = int(W * 0.07)
    max_tw   = W - pad_x * 2

    tmp = ImageDraw.Draw(Image.new("RGBA", (W, H)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    h_lines = _wrap(headline.upper(), h_font, max_tw, tmp)[:2]
    b_lines = (_wrap(body, b_font, max_tw, tmp)[:3] if body else [])
    gap    = max(6, int(h_lh * 0.2))
    block_h = (
        h_lh * len(h_lines) + gap * len(h_lines)
        + (b_lh * len(b_lines) + gap * len(b_lines) + gap if b_lines else 0)
    )
    text_y  = img_h + (band_h - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2      = ImageDraw.Draw(overlay)
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw, W//2, text_y, text_rgb)
    return Image.alpha_composite(canvas, overlay)


def _render_split(img, headline: str, body: str, h_font, b_font, brand_color: str) -> "Image.Image":
    from PIL import Image, ImageDraw

    W, H      = img.size
    panel_w   = int(W * 0.43)
    img_w     = W - panel_w

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # Brand-colour panel on left
    r, g, b = _hex_to_rgb(brand_color)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 0), (panel_w, H)], fill=(r, g, b, 255))

    # Image on right
    photo = _cover_crop(img.convert("RGBA"), img_w, H)
    canvas.paste(photo, (panel_w, 0))

    # Text centered in left panel
    text_rgb = _text_color(brand_color)
    pad_x    = int(panel_w * 0.09)
    max_tw   = panel_w - pad_x * 2
    x_center = panel_w // 2

    tmp = ImageDraw.Draw(Image.new("RGBA", (W, H)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    h_lines = _wrap(headline.upper(), h_font, max_tw, tmp)[:3]
    b_lines = (_wrap(body, b_font, max_tw, tmp)[:4] if body else [])
    gap     = max(6, int(h_lh * 0.2))
    block_h = (
        h_lh * len(h_lines) + gap * len(h_lines)
        + (b_lh * len(b_lines) + gap * len(b_lines) + gap if b_lines else 0)
    )
    text_y = (H - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(overlay)
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw, x_center, text_y, text_rgb)
    return Image.alpha_composite(canvas, overlay)


def _render_solid(img, headline: str, body: str, h_font, b_font, brand_color: str) -> "Image.Image":
    from PIL import Image, ImageDraw

    W, H = img.size

    # Solid brand-colour background with very faint image texture
    r, g, b = _hex_to_rgb(brand_color)
    canvas = Image.new("RGBA", (W, H), (r, g, b, 255))

    photo = img.convert("RGBA")
    # 12 % opacity texture layer
    texture = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    photo_resized = _cover_crop(photo, W, H)
    photo_resized.putalpha(30)
    texture.paste(photo_resized, (0, 0))
    canvas = Image.alpha_composite(canvas, texture)

    # Text centered on canvas
    text_rgb = _text_color(brand_color)
    pad_x    = int(W * 0.09)
    max_tw   = W - pad_x * 2

    tmp     = ImageDraw.Draw(Image.new("RGBA", (W, H)))
    h_lh    = _line_h(h_font, tmp)
    b_lh    = _line_h(b_font, tmp)
    h_lines = _wrap(headline.upper(), h_font, max_tw, tmp)[:3]
    b_lines = (_wrap(body, b_font, max_tw, tmp)[:4] if body else [])
    gap     = max(8, int(h_lh * 0.25))
    block_h = (
        h_lh * len(h_lines) + gap * len(h_lines)
        + (b_lh * len(b_lines) + gap * len(b_lines) + gap if b_lines else 0)
    )
    text_y = (H - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(overlay)
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw, W//2, text_y, text_rgb)
    return Image.alpha_composite(canvas, overlay)


# ── Cover-crop helper ────────────────────────────────────────────────────────

def _cover_crop(img: "Image.Image", target_w: int, target_h: int) -> "Image.Image":
    from PIL import Image
    src_w, src_h = img.size
    scale  = max(target_w / src_w, target_h / src_h)
    new_w  = max(int(src_w * scale), target_w)
    new_h  = max(int(src_h * scale), target_h)
    r      = img.resize((new_w, new_h), Image.LANCZOS)
    left   = (new_w - target_w) // 2
    top    = (new_h - target_h) // 2
    return r.crop((left, top, left + target_w, top + target_h))


# ── Main public function ─────────────────────────────────────────────────────

def overlay_creative(
    image_bytes: bytes,
    headline: str,
    sb: Client,
    *,
    body_text: str = "",
    brand_color: str = "",
    anthropic: Any = None,
    platform: str = "instagram",
) -> bytes:
    """
    Composite headline + body copy onto the image using a Claude-chosen layout template.
    Returns composited PNG bytes.  On any error returns original bytes unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        print("[image_overlay] Pillow not installed — skipping overlay", flush=True)
        return image_bytes

    try:
        color = (brand_color or _DEFAULT_COLOR).strip()
        if not color.startswith("#"):
            color = _DEFAULT_COLOR

        # Ask Claude for template + placement
        layout = _pick_layout(image_bytes, anthropic, platform) if anthropic else {"template": "band", "placement": "bottom"}
        template  = layout["template"]
        placement = layout["placement"]

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        W, H = img.size

        # Font sizes scaled to image width
        h_size = max(40, min(92, int(W * 0.068)))   # headline
        b_size = max(20, min(40, int(W * 0.030)))   # body

        # Narrower text zone for split template — shrink headline a bit
        if template == "split":
            h_size = max(32, int(h_size * 0.75))
            b_size = max(16, int(b_size * 0.85))

        fonts  = _fetch_fonts(sb)
        h_font = _pil_font(fonts["bold"],  h_size)
        b_font = _pil_font(fonts["light"], b_size)

        if template == "hero":
            result = _render_hero(img, headline, body_text, h_font, b_font, color, placement)
        elif template == "band":
            result = _render_band(img, headline, body_text, h_font, b_font, color)
        elif template == "split":
            result = _render_split(img, headline, body_text, h_font, b_font, color)
        else:  # solid
            result = _render_solid(img, headline, body_text, h_font, b_font, color)

        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(
            f"[image_overlay] {template} template applied "
            f"({len(data)//1024} KB, color={color})",
            flush=True,
        )
        return data

    except Exception as exc:
        print(f"[image_overlay] overlay failed, returning original: {exc}", flush=True)
        return image_bytes


# ── Backward-compatible wrapper ──────────────────────────────────────────────

def overlay_headline(
    image_bytes: bytes,
    headline: str,
    sb: Client,
    *,
    anthropic: Any = None,
) -> bytes:
    """Legacy entry point — calls overlay_creative with no body text."""
    return overlay_creative(image_bytes, headline, sb, anthropic=anthropic)

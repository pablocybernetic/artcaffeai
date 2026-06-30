"""
image_overlay.py
----------------
Composites branded text onto a marketing image using one of four layout templates.

Templates (Claude picks automatically):
  hero   — full-bleed image, dark gradient scrim, left-aligned headline + body
  band   — image fills top 60 %, solid brand-colour band fills bottom 40 %
  split  — image fills right 55 %, brand-colour panel fills left 45 %
  solid  — solid brand colour (no photo), centered typographic card

Font priority:
  1. Custom font uploaded to Supabase `fonts` bucket (Gotham, etc.)
  2. Clean sans-serif from the Ubuntu system fonts (LiberationSans / DejaVu)
  3. Pillow built-in default (last resort only)
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Any, Optional

from supabase import Client

FONTS_BUCKET   = os.environ.get("FONTS_BUCKET", "fonts")
_LAYOUT_MODEL  = "claude-haiku-4-5-20251001"
_DEFAULT_COLOR = "#1B3A2A"   # Artcaffe forest green

# ── Custom font preference (names in Supabase bucket) ────────────────────────
_BOLD_PREF = [
    "Gotham-Medium.otf", "Gotham-Bold.otf", "Gotham-Black.otf",
    "GaramondITCbyBT-Bold.otf", "Paris.otf",
]
_LIGHT_PREF = [
    "Gotham-Book.otf", "Gotham-Light.otf", "Lovelo_Line_Light.otf",
    "GaramondITCbyBT-Book.otf",
]

# ── System font fallback paths (Ubuntu / Debian VM) ──────────────────────────
_SYS_BOLD = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
_SYS_REGULAR = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
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
        print(f"[image_overlay] fonts in bucket: {available}", flush=True)
    except Exception as e:
        print(f"[image_overlay] could not list fonts bucket: {e}", flush=True)
        available = []

    def _load_first(candidates: list[str]) -> Optional[bytes]:
        for name in candidates:
            try:
                data = sb.storage.from_(FONTS_BUCKET).download(name)
                if data:
                    print(f"[image_overlay] loaded bucket font: {name}", flush=True)
                    return data
            except Exception:
                continue
        return None

    bold_pref  = _BOLD_PREF  + [n for n in available if any(k in n.lower() for k in ("bold","medium","heavy","black"))]
    light_pref = _LIGHT_PREF + [n for n in available if any(k in n.lower() for k in ("light","book","regular","thin"))]
    fallback   = list(dict.fromkeys(bold_pref + light_pref + available))

    bold  = _load_first(bold_pref)  or _load_first(fallback)
    light = _load_first(light_pref) or bold
    return {"bold": bold, "light": light}


def _pil_font(font_bytes: Optional[bytes], size: int, role: str = "bold"):
    """
    Load a PIL font at `size`.
    Priority: bucket bytes → system sans-serif → PIL default.
    """
    from PIL import ImageFont

    if font_bytes:
        try:
            return ImageFont.truetype(io.BytesIO(font_bytes), size)
        except Exception as e:
            print(f"[image_overlay] bucket font load failed: {e}", flush=True)

    # Try clean system sans-serif
    sys_paths = _SYS_BOLD if role == "bold" else _SYS_REGULAR
    for path in sys_paths:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                print(f"[image_overlay] using system font: {path}", flush=True)
                return f
            except Exception:
                continue

    # Last resort: PIL built-in (may be tiny; at least won't crash)
    print("[image_overlay] WARNING: falling back to PIL default font", flush=True)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
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
    r, g, b = _hex_to_rgb(bg_hex)
    return (240, 235, 228) if _luminance(r, g, b) < 140 else (18, 16, 14)


# ── Claude layout picker ─────────────────────────────────────────────────────

def _pick_layout(image_bytes: bytes, anthropic: Any, platform: str) -> dict:
    """
    Ask Claude Haiku to choose template + hero text placement.
    Falls back to band/bottom if the call fails.
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
                        "  hero  — full image, gradient scrim, text in bottom-left area\n"
                        "  band  — image top 60%, brand-colour band bottom with text\n"
                        "  split — image right half, colour panel left half with text\n"
                        "  solid — solid colour background, no photo visible\n\n"
                        "For hero, also choose placement: bottom (default) / center / top\n"
                        "Prefer 'band' for busy or complex food/product photos.\n"
                        "Reply ONLY with JSON: {\"template\":\"band\",\"placement\":\"bottom\"}"
                    )},
                ],
            }],
            timeout=20.0,
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{[^}]+\}', raw)
        result    = json.loads(m.group()) if m else {}
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
    x_left: int,
    y_top: int,
    text_rgb: tuple[int, int, int],
    align: str = "left",     # "left" or "center"
    shadow: bool = True,
) -> int:
    """
    Render headline (uppercase, bold) then body copy.
    Returns the y coordinate below the last rendered line.
    x_left is the left edge of the text zone; for center align it's the midpoint.
    """
    gap    = max(8, int(_line_h(h_font, draw) * 0.18))
    b_gap  = max(6, int(_line_h(b_font, draw) * 0.18))

    def _shadow_offsets():
        return [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3)
                if abs(dx) + abs(dy) <= 3 and (dx or dy)]

    def _draw_line(line: str, font, y: int, is_body: bool = False) -> int:
        bb = draw.textbbox((0, 0), line, font=font)
        lw = bb[2] - bb[0]
        lh = bb[3] - bb[1]

        if align == "center":
            x = x_left - lw // 2
        else:
            x = x_left

        if shadow:
            for dx, dy in _shadow_offsets():
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 160))

        draw.text((x, y), line, font=font, fill=(*text_rgb, 255))
        return y + lh + (b_gap if is_body else gap)

    h_lines = _wrap(headline.upper(), h_font, max_w, draw)[:3]
    y = y_top
    for line in h_lines:
        y = _draw_line(line, h_font, y)

    if body:
        y += gap  # extra spacer between headline and body
        b_lines = _wrap(body, b_font, max_w, draw)[:3]
        if len(b_lines) > 3:
            b_lines = b_lines[:3]
            b_lines[-1] = b_lines[-1].rstrip() + "…"
        for line in b_lines:
            y = _draw_line(line, b_font, y, is_body=True)

    return y


# ── Scrim helper ─────────────────────────────────────────────────────────────

def _draw_scrim(W: int, H: int, solid_top: int, fade_height: int = 80) -> "Image.Image":
    """
    Fast gradient scrim: solid dark from solid_top to H,
    with a smooth fade-in over fade_height px above solid_top.
    Uses draw.line (O(H)) not putpixel (O(H*W)).
    """
    from PIL import Image, ImageDraw
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(scrim)
    # Fade zone
    for i in range(fade_height):
        alpha = int(185 * (i / fade_height) ** 1.8)
        y = solid_top - fade_height + i
        if 0 <= y < H:
            d.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    # Solid zone (just one rectangle — very fast)
    if solid_top < H:
        d.rectangle([(0, solid_top), (W, H)], fill=(0, 0, 0, 185))
    return scrim


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


# ── Template renderers ───────────────────────────────────────────────────────

def _render_hero(img, headline, body, h_font, b_font, brand_color, placement):
    from PIL import Image, ImageDraw

    W, H   = img.size
    canvas = img.convert("RGBA").copy()

    pad_x  = int(W * 0.07)
    pad_y  = int(H * 0.05)
    max_tw = W - pad_x * 2

    # Measure full text block height
    tmp   = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh  = _line_h(h_font, tmp)
    b_lh  = _line_h(b_font, tmp)
    n_hl  = min(3, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl  = min(3, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap   = max(8, int(h_lh * 0.18))
    b_gap = max(6, int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )

    # Text Y based on placement (default bottom)
    if placement in ("top",):
        text_y = pad_y
    elif placement in ("upper-third",):
        text_y = int(H * 0.14)
    elif placement == "center":
        text_y = (H - block_h) // 2
    else:  # bottom / lower-third — default
        text_y = H - block_h - pad_y

    # Draw scrim — covers from (text_y - 2*pad_y) to bottom
    scrim_top = max(0, text_y - pad_y * 2)
    scrim     = _draw_scrim(W, H, scrim_top, fade_height=int(H * 0.12))
    canvas    = Image.alpha_composite(canvas, scrim)

    # Draw text left-aligned over scrim
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d       = ImageDraw.Draw(overlay)
    _draw_text_block(d, headline, body, h_font, b_font, max_tw, pad_x, text_y, (240, 235, 228), align="left")
    return Image.alpha_composite(canvas, overlay)


def _render_band(img, headline, body, h_font, b_font, brand_color):
    from PIL import Image, ImageDraw

    W, H   = img.size
    img_h  = int(H * 0.60)
    band_h = H - img_h

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    photo  = _cover_crop(img.convert("RGBA"), W, img_h)
    canvas.paste(photo, (0, 0))

    r, g, b = _hex_to_rgb(brand_color)
    d = ImageDraw.Draw(canvas)
    d.rectangle([(0, img_h), (W, H)], fill=(r, g, b, 255))

    # Tight divider line between image and band (lighter tint)
    lr, lg, lb = min(r+30,255), min(g+30,255), min(b+30,255)
    d.rectangle([(0, img_h), (W, img_h + 2)], fill=(lr, lg, lb, 180))

    text_rgb = _text_color(brand_color)
    pad_x    = int(W * 0.07)
    max_tw   = W - pad_x * 2

    tmp    = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    n_hl   = min(2, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl   = min(2, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap    = max(6, int(h_lh * 0.18))
    b_gap  = max(5, int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )
    text_y = img_h + (band_h - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2      = ImageDraw.Draw(overlay)
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw, pad_x, text_y, text_rgb, align="left", shadow=False)
    return Image.alpha_composite(canvas, overlay)


def _render_split(img, headline, body, h_font, b_font, brand_color):
    from PIL import Image, ImageDraw

    W, H    = img.size
    panel_w = int(W * 0.43)
    img_w   = W - panel_w

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    r, g, b = _hex_to_rgb(brand_color)
    d = ImageDraw.Draw(canvas)
    d.rectangle([(0, 0), (panel_w, H)], fill=(r, g, b, 255))

    photo = _cover_crop(img.convert("RGBA"), img_w, H)
    canvas.paste(photo, (panel_w, 0))

    text_rgb = _text_color(brand_color)
    pad_x    = int(panel_w * 0.10)
    max_tw   = panel_w - pad_x * 2

    tmp    = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    n_hl   = min(3, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl   = min(4, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap    = max(6, int(h_lh * 0.18))
    b_gap  = max(5, int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )
    text_y = (H - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(overlay)
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw, pad_x, text_y, text_rgb, align="left", shadow=False)
    return Image.alpha_composite(canvas, overlay)


def _render_solid(img, headline, body, h_font, b_font, brand_color):
    from PIL import Image, ImageDraw

    W, H = img.size
    r, g, b = _hex_to_rgb(brand_color)
    canvas = Image.new("RGBA", (W, H), (r, g, b, 255))

    # 12 % photo texture
    photo = _cover_crop(img.convert("RGBA"), W, H)
    photo.putalpha(30)
    canvas = Image.alpha_composite(canvas, photo)

    text_rgb = _text_color(brand_color)
    pad_x    = int(W * 0.09)
    max_tw   = W - pad_x * 2

    tmp    = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    n_hl   = min(3, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl   = min(4, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap    = max(8, int(h_lh * 0.22))
    b_gap  = max(6, int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )
    text_y = (H - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    _draw_text_block(d, headline, body, h_font, b_font, max_tw, W//2, text_y, text_rgb, align="center", shadow=False)
    return Image.alpha_composite(canvas, overlay)


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

        layout    = _pick_layout(image_bytes, anthropic, platform) if anthropic else {"template": "band", "placement": "bottom"}
        template  = layout["template"]
        placement = layout["placement"]

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        W, H = img.size

        # Font sizes — headline larger relative to image width
        h_size = max(44, min(96, int(W * 0.072)))
        b_size = max(20, min(38, int(W * 0.028)))

        # Narrower panel in split → slightly smaller text
        if template == "split":
            h_size = max(34, int(h_size * 0.72))
            b_size = max(16, int(b_size * 0.82))

        fonts  = _fetch_fonts(sb)
        h_font = _pil_font(fonts["bold"],  h_size, role="bold")
        b_font = _pil_font(fonts["light"], b_size, role="regular")

        if template == "hero":
            result = _render_hero(img, headline, body_text, h_font, b_font, color, placement)
        elif template == "band":
            result = _render_band(img, headline, body_text, h_font, b_font, color)
        elif template == "split":
            result = _render_split(img, headline, body_text, h_font, b_font, color)
        else:
            result = _render_solid(img, headline, body_text, h_font, b_font, color)

        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(f"[image_overlay] {template} template — {len(data)//1024} KB, color={color}", flush=True)
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

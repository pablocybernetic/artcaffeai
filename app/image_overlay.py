"""
image_overlay.py
----------------
Composites branded text onto a marketing image.

Compliant with Artcaffe Image Guidelines 2026, section 7:
  ✅  White bold text on naturally dark photo area (no modification to photo)
  ✅  Narrow semi-transparent DARK bar (60-70 % black) when photo is busy
  ❌  Full colour tint / brand-colour overlay — NEVER applied over photos

Templates (Claude picks by looking at the image):
  bar    — full-bleed photo + narrow dark bar (28% H) at bottom, white text
           → use when photo is busy / no clean dark area
  clean  — full-bleed photo, white text sits directly on a dark area of the image
           → use when photo already has a dark zone (shadow, dark background)
  split  — image right 55%, brand-colour panel left 45% — purely typographic zone
           → use for portrait/story formats where a text panel makes sense
  solid  — typographic card, brand colour background, no full-photo overlay
           → only when there is no usable photo (e.g. text-only campaigns)

Claude chooses font pair (headline + body) from the uploaded bucket fonts.
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


# ── Font loading ─────────────────────────────────────────────────────────────

def _load_font_map(sb: Client) -> dict[str, bytes]:
    """
    Load ALL fonts from the Supabase bucket.
    Returns {filename: bytes} for every font that downloads successfully.
    """
    try:
        listing = sb.storage.from_(FONTS_BUCKET).list("", {"limit": 100})
        names = [
            f["name"] for f in (listing or [])
            if f.get("name") and f["name"] != ".emptyFolderPlaceholder"
        ]
        print(f"[image_overlay] fonts in bucket: {names}", flush=True)
    except Exception as e:
        print(f"[image_overlay] font listing failed: {e}", flush=True)
        return {}

    font_map: dict[str, bytes] = {}
    for name in names:
        try:
            data = sb.storage.from_(FONTS_BUCKET).download(name)
            if data:
                font_map[name] = data
        except Exception as e:
            print(f"[image_overlay] could not load {name}: {e}", flush=True)
    print(f"[image_overlay] loaded {len(font_map)} fonts: {list(font_map)}", flush=True)
    return font_map


def _pil_font(font_bytes: Optional[bytes], size: int):
    """Load a PIL font from bytes. Falls back to PIL built-in only if bytes fail."""
    from PIL import ImageFont
    if font_bytes:
        try:
            return ImageFont.truetype(io.BytesIO(font_bytes), size)
        except Exception as e:
            print(f"[image_overlay] font load failed: {e}", flush=True)
    print("[image_overlay] WARNING: no bucket font available, using PIL default", flush=True)
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


# ── Claude design picker ─────────────────────────────────────────────────────

def _pick_design(
    image_bytes: bytes,
    anthropic: Any,
    platform: str,
    font_names: list[str],
) -> dict:
    """
    Ask Claude to look at the product image and decide:
      - layout template (bar / clean / split / solid)
      - text placement for clean (top / center / bottom)
      - headline_font — one of the uploaded font filenames
      - body_font     — a DIFFERENT uploaded font filename

    Returns dict with those four keys. Falls back to safe defaults if call fails.
    """
    # Safe defaults (use first two fonts available, or same font twice)
    default_h = font_names[0] if font_names else ""
    default_b = font_names[1] if len(font_names) > 1 else default_h

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()

        fonts_list = "\n".join(f"  - {n}" for n in font_names)

        prompt = f"""You are a brand designer creating a {platform} marketing creative for Artcaffe Coffee & Restaurant.

Artcaffe Image Guidelines 2026 — MUST FOLLOW:
- NEVER apply a brand-colour tint over a photograph
- Use only a narrow DARK (black) bar overlay when the photo needs text contrast

Look at this product photo and design the text overlay. Choose ONE template:

LAYOUT TEMPLATES:
  bar   — full-bleed photo + narrow dark bar (28% height) at BOTTOM, white text
          → DEFAULT: use when photo is busy or subject fills the whole frame
  clean — full-bleed photo, white text placed on an already-dark area of the image
          → only if photo has a naturally dark corner/zone with no important subject
  split — brand-colour panel left 45%, image right 55%
          → best for portrait/story format where a typographic panel makes sense
  solid — brand colour background, faint image texture, no photo overlay
          → only when image quality is very low or campaign is purely typographic

PLACEMENT (clean template only): top / center / bottom
  → where in the photo the naturally dark zone is

AVAILABLE FONTS (use EXACT filenames):
{fonts_list}

Rules:
- headline_font MUST be Gotham-Medium.otf or Gotham-Bold.otf (primary brand headline fonts)
  — only use another font if neither is in the list above
- body_font must be a DIFFERENT file from headline_font
- Good body font choices: Gotham-Book.otf, Gotham-Light.otf, Lovelo_Line_Light.otf

Reply ONLY with valid JSON (no markdown, no explanation):
{{"template":"bar","placement":"bottom","headline_font":"Gotham-Medium.otf","body_font":"Gotham-Light.otf"}}"""

        resp = anthropic.messages.create(
            model=_LAYOUT_MODEL,
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
            timeout=25.0,
        )
        raw = resp.content[0].text.strip()
        m   = re.search(r'\{[^}]+\}', raw, re.DOTALL)
        result = json.loads(m.group()) if m else {}

        template  = result.get("template", "hero")
        placement = result.get("placement", "center")
        h_font    = result.get("headline_font", default_h)
        b_font    = result.get("body_font", default_b)

        if template  not in ("bar", "clean", "split", "solid"): template  = "bar"
        if placement not in ("top", "center", "bottom"):       placement = "bottom"
        if h_font not in font_names: h_font = default_h
        if b_font not in font_names: b_font = default_b

        # Enforce brand rule: headline must be a Gotham bold/medium weight
        _HEADLINE_REQUIRED = ["Gotham-Medium.otf", "Gotham-Bold.otf"]
        approved_h = next((f for f in _HEADLINE_REQUIRED if f in font_names), None)
        if approved_h and h_font not in _HEADLINE_REQUIRED:
            print(f"[image_overlay] overriding headline font {h_font} → {approved_h}", flush=True)
            h_font = approved_h

        # Ensure body is always a different font from headline
        if h_font == b_font and len(font_names) > 1:
            b_font = next((f for f in font_names if f != h_font), b_font)

        print(f"[image_overlay] design: template={template} placement={placement} "
              f"headline={h_font} body={b_font}", flush=True)
        return {"template": template, "placement": placement,
                "headline_font": h_font, "body_font": b_font}

    except Exception as exc:
        print(f"[image_overlay] design pick failed ({exc}), using defaults", flush=True)
        _HEADLINE_REQUIRED = ["Gotham-Medium.otf", "Gotham-Bold.otf"]
        fallback_h = next((f for f in _HEADLINE_REQUIRED if f in font_names), default_h)
        fallback_b = next((f for f in font_names if f != fallback_h), default_b)
        return {"template": "bar", "placement": "bottom",
                "headline_font": fallback_h, "body_font": fallback_b}


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

def _render_bar(img, headline, body, h_font, b_font):
    """
    Full-bleed photo + narrow dark bar at bottom (28 % height, 65 % black opacity).
    Per brand guidelines: dark overlay bar only — never brand-colour tint over photo.
    White text inside the bar, left-aligned with left padding.
    """
    from PIL import Image, ImageDraw

    W, H     = img.size
    canvas   = _cover_crop(img.convert("RGBA"), W, H).copy()
    bar_h    = int(H * 0.28)
    bar_top  = H - bar_h
    pad_x    = int(W * 0.07)
    max_tw   = W - pad_x * 2

    # Fade + solid dark bar — no brand colour, pure dark for readability
    bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd  = ImageDraw.Draw(bar)
    fade_h = int(bar_h * 0.35)
    for i in range(fade_h):
        alpha = int(168 * (i / fade_h) ** 1.6)
        y = bar_top - fade_h + i
        if 0 <= y < H:
            bd.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    bd.rectangle([(0, bar_top), (W, H)], fill=(0, 0, 0, 168))  # ~66 % opacity
    canvas = Image.alpha_composite(canvas, bar)

    # Measure block to vertically center in bar
    tmp    = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    n_hl   = min(2, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl   = min(2, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap    = max(6, int(h_lh * 0.18))
    b_gap  = max(5, int(b_lh * 0.16))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )
    text_y = bar_top + (bar_h - block_h) // 2

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    _draw_text_block(d, headline, body, h_font, b_font, max_tw,
                     pad_x, text_y, (255, 255, 255), align="left", shadow=False)
    return Image.alpha_composite(canvas, overlay)


def _render_clean(img, headline, body, h_font, b_font, placement):
    """
    Full-bleed photo, white text placed directly on a naturally dark area.
    Minimal invisible scrim (only light shadow behind text for legibility).
    Per brand guidelines: photo untouched — no colour overlay.
    """
    from PIL import Image, ImageDraw

    W, H   = img.size
    canvas = _cover_crop(img.convert("RGBA"), W, H).copy()
    pad_x  = int(W * 0.07)
    pad_y  = int(H * 0.06)
    max_tw = W - pad_x * 2

    tmp    = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh   = _line_h(h_font, tmp)
    b_lh   = _line_h(b_font, tmp)
    n_hl   = min(3, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl   = min(2, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap    = max(8, int(h_lh * 0.20))
    b_gap  = max(6, int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )

    if placement == "top":
        text_y = pad_y
    elif placement == "center":
        text_y = (H - block_h) // 2
    else:  # bottom
        text_y = H - block_h - pad_y

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    _draw_text_block(d, headline, body, h_font, b_font, max_tw,
                     pad_x, text_y, (255, 255, 255), align="left", shadow=True)
    return Image.alpha_composite(canvas, overlay)


def _render_split(img, headline, body, h_font, b_font, brand_color):
    """
    Image right 55%, brand-colour panel left 45%.
    The colour panel is a pure typography zone — no overlay on the photo itself.
    Per brand guidelines: colour only where there is no photo behind it.
    """
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
    _draw_text_block(d2, headline, body, h_font, b_font, max_tw,
                     pad_x, text_y, text_rgb, align="left", shadow=False)
    return Image.alpha_composite(canvas, overlay)


def _render_solid(img, headline, body, h_font, b_font, brand_color):
    """
    Typographic card — brand colour background, no photo overlay.
    Faint 12 % photo texture for depth without obscuring the colour identity.
    Only use when there is no real photo to show (text-only campaigns).
    """
    from PIL import Image, ImageDraw

    W, H = img.size
    r, g, b = _hex_to_rgb(brand_color)
    canvas = Image.new("RGBA", (W, H), (r, g, b, 255))

    # 12 % photo texture — just enough to hint at depth
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

        # Load all bucket fonts first — Claude needs the list to pick from
        font_map   = _load_font_map(sb)
        font_names = list(font_map.keys())

        # Claude sees the image + font list and decides layout + font pair
        if anthropic and font_names:
            design = _pick_design(image_bytes, anthropic, platform, font_names)
        else:
            design = {
                "template": "bar", "placement": "bottom",
                "headline_font": font_names[0] if font_names else "",
                "body_font":     font_names[1] if len(font_names) > 1 else (font_names[0] if font_names else ""),
            }

        template   = design["template"]
        placement  = design["placement"]
        h_fname    = design["headline_font"]
        b_fname    = design["body_font"]

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        W, H = img.size

        # Font sizes — headline larger relative to image width
        h_size = max(44, min(96, int(W * 0.072)))
        b_size = max(20, min(38, int(W * 0.028)))

        # Narrower panel in split → slightly smaller text
        if template == "split":
            h_size = max(34, int(h_size * 0.72))
            b_size = max(16, int(b_size * 0.82))

        h_font = _pil_font(font_map.get(h_fname), h_size)
        b_font = _pil_font(font_map.get(b_fname), b_size)

        if template == "bar":
            result = _render_bar(img, headline, body_text, h_font, b_font)
        elif template == "clean":
            result = _render_clean(img, headline, body_text, h_font, b_font, placement)
        elif template == "split":
            result = _render_split(img, headline, body_text, h_font, b_font, color)
        else:
            result = _render_solid(img, headline, body_text, h_font, b_font, color)

        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(f"[image_overlay] {template}/{placement} h={h_fname} b={b_fname} "
              f"— {len(data)//1024} KB", flush=True)
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

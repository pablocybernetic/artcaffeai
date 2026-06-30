"""
image_overlay.py
----------------
Composites branded text onto a marketing image using one of four layout templates.

Templates (Claude picks automatically by looking at the image):
  hero   — full-bleed image, brand-colour tint over lower half, centered text
  band   — image fills top 68 %, solid brand-colour band bottom 32 %
  split  — image right 55 %, colour panel left 45 % with text
  solid  — solid brand colour + faint photo texture, centered type

Claude also chooses the font pair (headline + body) from the uploaded bucket fonts.
No system fonts are used — only fonts uploaded to the Supabase `fonts` bucket.
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
      - layout template (hero / band / split / solid)
      - text placement for hero (center / bottom / top)
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

Look at this product photo and design the text overlay. Choose:

LAYOUT TEMPLATES:
  hero  — full-bleed image, brand-colour tint over lower half, bold centered text
          → best for clean product shots on plain/white backgrounds
  band  — image top 68%, solid brand-colour panel bottom 32% with text
          → best for busy scenes or when the subject fills the whole frame
  split — colour panel left 45%, image right 55%
          → best for portrait/story format
  solid — solid brand colour fills card, faint image texture behind text
          → best when image quality is low or you want a typographic card

PLACEMENT (hero only): center / bottom / top

AVAILABLE FONTS (use EXACT filenames):
{fonts_list}

Rules:
- headline_font MUST be Gotham-Medium.otf or Gotham-Bold.otf (primary brand headline fonts)
  — only use another font if neither is in the list above
- body_font must be a DIFFERENT file from headline_font
- Good body font choices: Gotham-Book.otf, Gotham-Light.otf, Lovelo_Line_Light.otf

Reply ONLY with valid JSON (no markdown, no explanation):
{{"template":"hero","placement":"center","headline_font":"Gotham-Medium.otf","body_font":"Gotham-Light.otf"}}"""

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

        if template  not in ("hero", "band", "split", "solid"): template  = "hero"
        if placement not in ("top", "center", "bottom"):        placement = "center"
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
        return {"template": "hero", "placement": "center",
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

def _render_hero(img, headline, body, h_font, b_font, brand_color, placement):
    """
    Full-bleed image with a brand-colour tint wash over the lower half
    and a strong gradient fade — Pomelli-style editorial look.
    Text is centered for maximum impact on the tinted zone.
    """
    from PIL import Image, ImageDraw

    W, H   = img.size
    canvas = _cover_crop(img.convert("RGBA"), W, H).copy()

    r, g, b = _hex_to_rgb(brand_color)

    # ── Brand-colour tint wash over bottom ~55 % of image ────────────────────
    tint = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td   = ImageDraw.Draw(tint)
    fade_start = int(H * 0.35)       # tint starts fading in here
    fade_end   = int(H * 0.62)       # fully opaque brand colour from here down
    max_alpha  = 210                  # solid zone opacity (high for readability)

    for i in range(fade_end - fade_start):
        alpha = int(max_alpha * (i / (fade_end - fade_start)) ** 1.4)
        y = fade_start + i
        td.line([(0, y), (W, y)], fill=(r, g, b, alpha))
    td.rectangle([(0, fade_end), (W, H)], fill=(r, g, b, max_alpha))
    canvas = Image.alpha_composite(canvas, tint)

    # ── Measure text block ────────────────────────────────────────────────────
    pad_x  = int(W * 0.08)
    pad_y  = int(H * 0.05)
    max_tw = W - pad_x * 2

    tmp   = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    h_lh  = _line_h(h_font, tmp)
    b_lh  = _line_h(b_font, tmp)
    n_hl  = min(3, len(_wrap(headline.upper(), h_font, max_tw, tmp)))
    n_bl  = min(2, len(_wrap(body, b_font, max_tw, tmp))) if body else 0
    gap   = max(10, int(h_lh * 0.20))
    b_gap = max(6,  int(b_lh * 0.18))
    block_h = (
        h_lh * n_hl + gap * n_hl
        + (b_lh * n_bl + b_gap * n_bl + gap if n_bl else 0)
    )

    # Place text in the tinted zone, centered horizontally
    tint_center_y = fade_end + (H - fade_end) // 2
    text_y = max(fade_end + pad_y, tint_center_y - block_h // 2)

    text_rgb = _text_color(brand_color)
    overlay  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d        = ImageDraw.Draw(overlay)
    _draw_text_block(
        d, headline, body, h_font, b_font,
        max_tw, W // 2, text_y, text_rgb,
        align="center", shadow=False,
    )
    return Image.alpha_composite(canvas, overlay)


def _render_band(img, headline, body, h_font, b_font, brand_color):
    from PIL import Image, ImageDraw

    W, H   = img.size
    img_h  = int(H * 0.68)   # image takes 68 %, band only 32 %
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

        # Load all bucket fonts first — Claude needs the list to pick from
        font_map   = _load_font_map(sb)
        font_names = list(font_map.keys())

        # Claude sees the image + font list and decides layout + font pair
        if anthropic and font_names:
            design = _pick_design(image_bytes, anthropic, platform, font_names)
        else:
            design = {
                "template": "hero", "placement": "center",
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

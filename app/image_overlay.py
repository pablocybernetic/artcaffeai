"""
image_overlay.py
----------------
Composites branded text onto a marketing image.

Compliant with Artcaffe Image Guidelines 2026, section 7:
  ✅  White bold text on naturally dark photo area (no modification to photo)
  ✅  Narrow semi-transparent DARK bar (60-70 % black) when photo is busy
  ❌  Full colour tint / brand-colour overlay — NEVER applied over photos

Object/subject awareness (two-layer):
  1. _brightness_zones() — pure PIL, zero cost: divides image into a 3×3 grid,
     finds the brightest zone (likely where food is) and darkest zone (safe for
     text). Always runs as a fast baseline.
  2. _pick_design() — Claude Haiku vision: looks at the actual image, returns
     subject_zone and bar_position alongside template + font choices.
     Claude's answer overrides the PIL heuristic when available.

Templates (Claude picks by looking at the image):
  bar    — full-bleed photo + narrow dark bar (28% H), white text.
           Bar placed at the OPPOSITE end from the main subject.
  clean  — full-bleed photo, white text sits directly on a dark area.
           Placement driven by darkest zone / subject location.
  split  — image right 55%, brand-colour panel left 45% — purely typographic zone
  solid  — typographic card, brand colour background, no full-photo overlay

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
from pantone_2026 import all_hex_colors, palette_prompt_block

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


# ── Object / brightness analysis ─────────────────────────────────────────────

def _brightness_zones(image_bytes: bytes) -> dict:
    """
    Pure-PIL subject localisation — no network calls, runs in < 5 ms.

    Divides the image into a 3×3 grid of equal cells, computes mean luminance
    per cell, then aggregates into horizontal thirds (top / mid / bottom) and
    vertical thirds (left / mid / right).

    Heuristic for food photography:
      - subject_zone   — brightest horizontal third (well-lit food)
      - darkest_zone   — darkest horizontal third (safest for clean template text)
      - bar_position   — 'top' when subject is in bottom third, else 'bottom'
      - placement      — best edge for clean template ('top' or 'bottom')

    Returns a dict with those keys plus zone_scores for debugging.
    Falls back to safe defaults if PIL fails for any reason.
    """
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((90, 90), Image.BILINEAR)
        px  = list(img.getdata())
        W, H = img.size

        t = H // 3   # rows per third

        # Collect pixel values per horizontal third
        def _row_mean(y0: int, y1: int) -> float:
            vals = [px[y * W + x] for y in range(y0, min(y1, H)) for x in range(W)]
            return sum(vals) / max(len(vals), 1)

        scores = {
            "top":    _row_mean(0,   t),
            "mid":    _row_mean(t,   2 * t),
            "bottom": _row_mean(2*t, H),
        }

        # Also score left / right halves (helps detect split compositions)
        def _col_mean(x0: int, x1: int) -> float:
            vals = [px[y * W + x] for y in range(H) for x in range(x0, min(x1, W))]
            return sum(vals) / max(len(vals), 1)

        horiz = {"left": _col_mean(0, W//2), "right": _col_mean(W//2, W)}

        subject_zone  = max(scores, key=scores.__getitem__)
        darkest_zone  = min(scores, key=scores.__getitem__)

        # Bar goes to the opposite end from the subject
        if subject_zone == "bottom":
            bar_position = "top"
        else:
            bar_position = "bottom"

        # clean placement: use the darkest edge (top or bottom), never mid
        if darkest_zone in ("top", "bottom"):
            placement = darkest_zone
        elif scores["top"] <= scores["bottom"]:
            placement = "top"
        else:
            placement = "bottom"

        print(
            f"[image_overlay] brightness top={scores['top']:.0f} "
            f"mid={scores['mid']:.0f} bot={scores['bottom']:.0f} "
            f"→ subject={subject_zone} bar={bar_position} placement={placement}",
            flush=True,
        )
        return {
            "subject_zone": subject_zone,
            "bar_position": bar_position,
            "placement":    placement,
            "zone_scores":  scores,
            "horiz_scores": horiz,
        }
    except Exception as exc:
        print(f"[image_overlay] brightness analysis failed: {exc}", flush=True)
        return {
            "subject_zone": "center",
            "bar_position": "bottom",
            "placement":    "bottom",
            "zone_scores":  {},
            "horiz_scores": {},
        }


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
      - subject_zone  — where the main food subject is (top / center / bottom)
      - bar_position  — which edge to place the dark bar (top / bottom)
      - placement     — for clean template, where the dark zone is (top / center / bottom)
      - headline_font — one of the uploaded font filenames
      - body_font     — a DIFFERENT uploaded font filename
      - palette_name  — one of the Pantone 2026 palette names
      - palette_color — one hex value from that palette (for split/solid backgrounds)

    Returns dict with those eight keys. Falls back to safe defaults if call fails.
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
        palette_block = palette_prompt_block()

        prompt = f"""You are a brand designer creating a {platform} marketing creative for Artcaffe Coffee & Restaurant.

Artcaffe Image Guidelines 2026 — MUST FOLLOW:
- NEVER apply a brand-colour tint over a photograph
- Use only a narrow DARK (black) bar overlay when the photo needs text contrast

Look at this product photo and design the text overlay. Choose ONE template:

LAYOUT TEMPLATES — pick based on image type and visual composition:

  bar   — full-bleed photo + narrow dark bar (25% height), white text in bar
          → USE FOR: busy close-up food shots, complex textured backgrounds,
            dramatic shots where the subject fills the whole frame, dark-tone photography
          → AVOID FOR: clean product-on-white or neutral-background photos

  clean — full-bleed photo, white text placed on an already-dark area of the photo
          → USE FOR: moody/atmospheric photography with a genuinely dark corner or zone,
            low-key lighting, dark backgrounds
          → REQUIRES a naturally dark area — do NOT use on white or bright backgrounds

  split — solid colour panel (40%) on one side, photo on the other
          → USE FOR: product-on-white shots, clean flat-lays, minimalist photography,
            any time you want a strong brand identity zone alongside the product
          → Works at ANY aspect ratio — not limited to portrait

  solid — full brand-colour background, photo at 12% texture opacity
          → USE FOR: clean product shots on white/neutral backgrounds, bold announcement
            posts, when the brand colour should dominate the visual
          → Especially effective for food-on-white and simple product photography

SELECTION GUIDE (follow strictly):
  - Product or food on WHITE or NEUTRAL background → "solid" or "split" (not bar)
  - Food filling the frame with complex/dark background → "bar"
  - Photo with a genuine dark zone and no subject in it → "clean"
  - When torn between bar and split → choose "split"

PLACEMENT (clean template only): top / center / bottom
  → where in the photo the naturally dark zone is

SUBJECT LOCATION — where is the main food or product in this photo?
  subject_zone: top / center / bottom

BAR POSITION — for the bar template only, place the bar on the OPPOSITE side from the subject.
  bar_position: bottom (subject is top or center) / top (subject is at the bottom)

AVAILABLE FONTS (use EXACT filenames):
{fonts_list}

Rules:
- headline_font MUST be Gotham-Medium.otf or Gotham-Bold.otf (primary brand headline fonts)
  — only use another font if neither is in the list above
- body_font must be a DIFFERENT file from headline_font
- Good body font choices: Gotham-Book.otf, Gotham-Light.otf, Lovelo_Line_Light.otf

{palette_block}

ACCENT COLOUR RULES:
- palette_name: pick the palette whose mood best matches the photo's food/drink subject and lighting
- palette_color: pick ONE hex from that palette — choose a colour that complements the photo
  → For DARK photos (evening, moody): prefer darker shades from Glamour & Gleam or Light & Shadow
  → For BRIGHT, WARM photos (coffee, pastries): prefer Take a Break or Comfort Zone
  → For FRESH, VIVID photos (cocktails, fruit): prefer Tropic Tonalities
  → Only split/solid templates use palette_color; bar/clean ignore it

Reply ONLY with valid JSON (no markdown, no explanation):
{{"template":"bar","placement":"bottom","subject_zone":"center","bar_position":"bottom","headline_font":"Gotham-Medium.otf","body_font":"Gotham-Light.otf","palette_name":"Take a Break","palette_color":"#B8916E"}}"""

        resp = anthropic.messages.create(
            model=_LAYOUT_MODEL,
            max_tokens=180,
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

        template     = result.get("template", "bar")
        placement    = result.get("placement", "bottom")
        subject_zone = result.get("subject_zone", "center")
        bar_position = result.get("bar_position", "bottom")
        h_font       = result.get("headline_font", default_h)
        b_font       = result.get("body_font", default_b)

        if template     not in ("bar", "clean", "split", "solid"): template     = "bar"
        if placement    not in ("top", "center", "bottom"):        placement    = "bottom"
        if subject_zone not in ("top", "center", "bottom"):        subject_zone = "center"
        if bar_position not in ("top", "bottom"):                  bar_position = "bottom"
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

        # Palette colour — validate it's a known 2026 hex; else None (caller falls back to brand colour)
        raw_palette_color = result.get("palette_color", "")
        valid_hexes = set(all_hex_colors())
        palette_color = raw_palette_color if raw_palette_color in valid_hexes else None
        palette_name  = result.get("palette_name", "")

        print(
            f"[image_overlay] design: template={template} subject={subject_zone} "
            f"bar={bar_position} placement={placement} h={h_font} b={b_font} "
            f"palette={palette_name} accent={palette_color}",
            flush=True,
        )
        return {
            "template":      template,
            "placement":     placement,
            "subject_zone":  subject_zone,
            "bar_position":  bar_position,
            "headline_font": h_font,
            "body_font":     b_font,
            "palette_name":  palette_name,
            "palette_color": palette_color,
        }

    except Exception as exc:
        print(f"[image_overlay] design pick failed ({exc}), using defaults", flush=True)
        _HEADLINE_REQUIRED = ["Gotham-Medium.otf", "Gotham-Bold.otf"]
        fallback_h = next((f for f in _HEADLINE_REQUIRED if f in font_names), default_h)
        fallback_b = next((f for f in font_names if f != fallback_h), default_b)
        return {
            "template":      "bar",
            "placement":     "bottom",
            "subject_zone":  "center",
            "bar_position":  "bottom",
            "headline_font": fallback_h,
            "body_font":     fallback_b,
            "palette_name":  "",
            "palette_color": None,
        }


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


def _draw_positioned_text(
    draw,
    text: str,
    font,
    x: int,
    y: int,
    max_w: int,
    text_rgb: tuple[int, int, int],
    align: str = "left",
    shadow: bool = True,
) -> int:
    """
    Render one wrapped text block at an explicit (x, y) — used by the freeform
    layout editor where headline and body move independently, unlike
    _draw_text_block's fixed headline-then-body-below-it flow.
    x is the left edge for "left" align, or the horizontal midpoint for "center".
    Returns the y coordinate below the last rendered line.
    """
    if not text:
        return y

    lines = _wrap(text, font, max_w, draw)[:4]
    line_h = _line_h(font, draw)
    gap = max(6, int(line_h * 0.18))

    def _shadow_offsets():
        return [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3)
                if abs(dx) + abs(dy) <= 3 and (dx or dy)]

    cur_y = y
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=font)
        lw = bb[2] - bb[0]
        lh = bb[3] - bb[1]
        lx = x - lw // 2 if align == "center" else x

        if shadow:
            for dx, dy in _shadow_offsets():
                draw.text((lx + dx, cur_y + dy), line, font=font, fill=(0, 0, 0, 160))
        draw.text((lx, cur_y), line, font=font, fill=(*text_rgb, 255))
        cur_y += lh + gap

    return cur_y


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

def _render_bar(img, headline, body, h_font, b_font, bar_position: str = "bottom", accent_color: str | None = None):
    """
    Full-bleed photo + narrow bar (25-28% height).
    accent_color: optional hex for a branded/Pantone bar; None → near-black.
    bar_position: 'bottom' (default) or 'top'.
    Per brand guidelines: no full brand-colour tint over photo — bar only.
    """
    from PIL import Image, ImageDraw

    if accent_color:
        br, bg, bb = _hex_to_rgb(accent_color)
        solid_alpha = 230
    else:
        br, bg, bb = 0, 0, 0
        solid_alpha = 168

    W, H   = img.size
    canvas = _cover_crop(img.convert("RGBA"), W, H).copy()
    bar_h  = int(H * 0.28)
    pad_x  = int(W * 0.07)
    max_tw = W - pad_x * 2
    fade_h = int(bar_h * 0.35)

    bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd  = ImageDraw.Draw(bar)

    if bar_position == "top":
        bd.rectangle([(0, 0), (W, bar_h)], fill=(br, bg, bb, solid_alpha))
        for i in range(fade_h):
            alpha = int(solid_alpha * ((fade_h - i) / fade_h) ** 1.6)
            y = bar_h + i
            if 0 <= y < H:
                bd.line([(0, y), (W, y)], fill=(br, bg, bb, alpha))
        bar_solid_y = 0
    else:
        bar_solid_y = H - bar_h
        for i in range(fade_h):
            alpha = int(solid_alpha * (i / fade_h) ** 1.6)
            y = bar_solid_y - fade_h + i
            if 0 <= y < H:
                bd.line([(0, y), (W, y)], fill=(br, bg, bb, alpha))
        bd.rectangle([(0, bar_solid_y), (W, H)], fill=(br, bg, bb, solid_alpha))

    canvas = Image.alpha_composite(canvas, bar)

    # Measure text block to vertically center it inside the solid zone
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
    text_y = bar_solid_y + (bar_h - block_h) // 2

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
    template_override: str = "",
) -> bytes:
    """
    Composite headline + body copy onto the image using a Claude-chosen layout template.
    template_override: force a specific template ("bar"|"clean"|"split"|"solid") and skip Claude.
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

        # Layer 1 — fast PIL brightness analysis (always runs, no API cost)
        zones = _brightness_zones(image_bytes)

        _HEADLINE_REQUIRED = ["Gotham-Medium.otf", "Gotham-Bold.otf"]
        default_h = font_names[0] if font_names else ""
        default_b = font_names[1] if len(font_names) > 1 else default_h
        approved_h = next((f for f in _HEADLINE_REQUIRED if f in font_names), default_h)
        approved_b = next((f for f in font_names if f != approved_h), default_b)

        if template_override and template_override in ("bar", "clean", "split", "solid"):
            # Forced template — skip Claude entirely
            design = {
                "template":      template_override,
                "placement":     zones["placement"],
                "subject_zone":  zones["subject_zone"],
                "bar_position":  zones["bar_position"],
                "headline_font": approved_h,
                "body_font":     approved_b,
                "palette_name":  "",
                "palette_color": None,
            }
            print(f"[image_overlay] forced template={template_override}", flush=True)
        elif anthropic and font_names:
            # Layer 2 — Claude vision; overrides brightness heuristic
            design = _pick_design(image_bytes, anthropic, platform, font_names)
            design.setdefault("bar_position", zones["bar_position"])
            design.setdefault("subject_zone", zones["subject_zone"])
            design.setdefault("placement",    zones["placement"])
        else:
            design = {
                "template":      "bar",
                "placement":     zones["placement"],
                "subject_zone":  zones["subject_zone"],
                "bar_position":  zones["bar_position"],
                "headline_font": approved_h,
                "body_font":     approved_b,
                "palette_name":  "",
                "palette_color": None,
            }

        template      = design["template"]
        placement     = design["placement"]
        bar_position  = design["bar_position"]
        h_fname       = design["headline_font"]
        b_fname       = design["body_font"]
        # Pantone 2026 palette accent — overrides static brand colour on split/solid
        palette_color = design.get("palette_color")
        accent_color  = palette_color if palette_color else color

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
            # Use Pantone palette color for the bar when it's dark (luminance < 80)
            bar_accent = None
            if palette_color:
                pr, pg, pb = _hex_to_rgb(palette_color)
                if _luminance(pr, pg, pb) < 80:
                    bar_accent = palette_color
            result = _render_bar(img, headline, body_text, h_font, b_font, bar_position, bar_accent)
        elif template == "clean":
            result = _render_clean(img, headline, body_text, h_font, b_font, placement)
        elif template == "split":
            result = _render_split(img, headline, body_text, h_font, b_font, accent_color)
        else:
            result = _render_solid(img, headline, body_text, h_font, b_font, accent_color)

        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        palette_name = design.get("palette_name", "")
        print(
            f"[image_overlay] {template} bar={bar_position} placement={placement} "
            f"h={h_fname} b={b_fname} palette={palette_name!r} accent={accent_color} "
            f"— {len(data)//1024} KB",
            flush=True,
        )
        return data

    except Exception as exc:
        print(f"[image_overlay] overlay failed, returning original: {exc}", flush=True)
        return image_bytes


# ── Freeform layout — user-positioned text, no Claude call ──────────────────

def overlay_freeform(
    image_bytes: bytes,
    sb: Client,
    *,
    headline_text: str = "",
    headline_x_pct: float = 0.07,
    headline_y_pct: float = 0.70,
    headline_size_pct: float = 0.072,
    headline_color: str = "#FFFFFF",
    headline_align: str = "left",
    headline_font: str = "",
    body_text: str = "",
    body_x_pct: float = 0.07,
    body_y_pct: float = 0.85,
    body_size_pct: float = 0.028,
    body_color: str = "#FFFFFF",
    body_align: str = "left",
    body_font: str = "",
    scrim_position: str = "bottom",   # "top" | "bottom" | "none"
    scrim_height_pct: float = 0.35,
    scrim_opacity: float = 0.65,
) -> bytes:
    """
    Composite headline + body text at explicit, caller-chosen positions/styles.
    Used by the Assets page's drag-to-position layout editor — no Claude call,
    no template auto-selection, just deterministic placement. Returns
    composited PNG bytes; on any error returns the original bytes unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        print("[image_overlay] Pillow not installed — skipping overlay", flush=True)
        return image_bytes

    try:
        from PIL import ImageDraw

        font_map = _load_font_map(sb)
        font_names = list(font_map.keys())

        _HEADLINE_REQUIRED = ["Gotham-Medium.otf", "Gotham-Bold.otf"]
        default_h = font_names[0] if font_names else ""
        default_b = font_names[1] if len(font_names) > 1 else default_h
        auto_h = next((f for f in _HEADLINE_REQUIRED if f in font_names), default_h)
        h_fname = headline_font if headline_font in font_names else auto_h
        auto_b = next((f for f in font_names if f != h_fname), default_b)
        b_fname = body_font if body_font in font_names else auto_b

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        W, H = img.size
        canvas = img.convert("RGBA")

        if scrim_position in ("top", "bottom") and scrim_opacity > 0:
            scrim_h  = max(1, int(H * scrim_height_pct))
            alpha    = int(255 * min(max(scrim_opacity, 0.0), 1.0))
            fade_h   = max(1, int(scrim_h * 0.35))
            scrim    = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sd       = ImageDraw.Draw(scrim)
            if scrim_position == "top":
                sd.rectangle([(0, 0), (W, scrim_h)], fill=(0, 0, 0, alpha))
                for i in range(fade_h):
                    a = int(alpha * ((fade_h - i) / fade_h) ** 1.6)
                    y = scrim_h + i
                    if 0 <= y < H:
                        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
            else:
                solid_y = H - scrim_h
                for i in range(fade_h):
                    a = int(alpha * (i / fade_h) ** 1.6)
                    y = solid_y - fade_h + i
                    if 0 <= y < H:
                        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
                sd.rectangle([(0, solid_y), (W, H)], fill=(0, 0, 0, alpha))
            canvas = Image.alpha_composite(canvas, scrim)

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        if headline_text:
            h_size = max(16, int(W * headline_size_pct))
            h_font = _pil_font(font_map.get(h_fname), h_size)
            h_rgb  = _hex_to_rgb(headline_color) if headline_color.startswith("#") else _hex_to_rgb(_DEFAULT_COLOR)
            hx     = int(W * headline_x_pct)
            hy     = int(H * headline_y_pct)
            max_w  = int(W * 0.86) if headline_align == "center" else W - hx - int(W * 0.05)
            _draw_positioned_text(d, headline_text.upper(), h_font, hx, hy, max_w, h_rgb, headline_align, shadow=True)

        if body_text:
            b_size = max(12, int(W * body_size_pct))
            b_font = _pil_font(font_map.get(b_fname), b_size)
            b_rgb  = _hex_to_rgb(body_color) if body_color.startswith("#") else _hex_to_rgb(_DEFAULT_COLOR)
            bx     = int(W * body_x_pct)
            by     = int(H * body_y_pct)
            max_w  = int(W * 0.86) if body_align == "center" else W - bx - int(W * 0.05)
            _draw_positioned_text(d, body_text, b_font, bx, by, max_w, b_rgb, body_align, shadow=True)

        result = Image.alpha_composite(canvas, overlay)
        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        print(f"[image_overlay] freeform render — {len(data)//1024} KB", flush=True)
        return data

    except Exception as exc:
        print(f"[image_overlay] freeform overlay failed, returning original: {exc}", flush=True)
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

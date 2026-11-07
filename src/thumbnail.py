"""Stage 8 - per-video thumbnail generator.

Every thumbnail used to come out of one fixed template - same dark brand fill,
same giant number right-of-centre - so a channel page read as one image
repeated N times. Two things vary now, both driven by the channel config so a
channel's look is tunable without touching this file:

  1. PALETTE. Picked by hashing the video title into c.THUMB_PALETTES (see
     config/channels/*.py). Deterministic: the same title always renders the
     same thumbnail, different titles land on different backgrounds, and each
     channel's palette family is disjoint from the other's so Clear Cents and
     Mind Unpacked never read as the same brand. The hash is sha256, NOT
     Python's hash() - the builtin is salted per process, which would make the
     choice differ between runs.

  2. FOCAL TREATMENT. Still a big number when the copy has a strong one, but
     drawn in one of four styles (solid / outline / slab / shadow) carried by
     the palette, so two number videos on the same channel don't match either.
     With no strong number there is no number at all: the copy becomes a stack
     of 2-4 large keyword words in the same per-palette treatment, rather than
     a token digit nobody asked for.

Everything else the old template got right is kept: the bundled display face
(c.THUMB_FONT under assets/fonts/), heavy weight and stroke for small-size
legibility, 3-5 words maximum, a left accent bar and bottom-left wordmark, and
a hard guarantee that nothing is drawn into the bottom-right 300x120px zone
YouTube overlays with its duration badge.

Output:
  thumbnail.jpg
"""
import hashlib
import math
import re

from PIL import Image, ImageDraw, ImageFont

from .config import config
from .util import get_logger, stage_path

log = get_logger("thumbnail")

OUTPUT_NAME = "thumbnail.jpg"
W, H = 1280, 720

# Bottom-right zone YouTube overlays with its own UI (duration badge etc) -
# nothing we draw should land in here.
RESERVED_ZONE = (W - 300, H - 120, W, H)

# Fallback palette, used only if a channel config defines no THUMB_PALETTES.
DEFAULT_BG_STYLE = "vertical"
DEFAULT_FOCAL_STYLE = "solid"

# A number worth featuring: optional $, digits with thousands separators or a
# decimal, optional %/K/M/X suffix. Anything longer than MAX_FOCAL_CHARS can't
# be set large enough to work at feed size, so it's treated as "no number" and
# the keyword stack takes over instead.
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|K|M|X)?", re.IGNORECASE)
MAX_FOCAL_CHARS = 8

# Dropped when reducing a title to keyword words.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
    "for", "from", "get", "got", "had", "has", "have", "how", "i", "in", "into",
    "is", "it", "its", "me", "my", "no", "not", "of", "off", "on", "one", "or",
    "our", "out", "own", "so", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "up", "us", "was", "we", "were", "what",
    "when", "why", "will", "with", "you", "your",
}
MIN_KEYWORDS = 2
MAX_KEYWORDS = 4


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --- palette selection -------------------------------------------------------


def _palettes(c):
    """The channel's palette list, or a one-entry list rebuilt from its base
    THUMB_BG/ACCENT/TEXT so a channel config without THUMB_PALETTES still
    renders (just without the variety)."""
    palettes = getattr(c, "THUMB_PALETTES", None)
    if palettes:
        return palettes
    log.warning("%s defines no THUMB_PALETTES; falling back to the flat base "
                "brand colours (every thumbnail will look the same)", c.NAME)
    base = _hex_to_rgb(c.THUMB_BG)
    return [{
        "name": "base",
        "bg": c.THUMB_BG,
        "bg2": "#%02X%02X%02X" % tuple(max(0, int(v * 0.45)) for v in base),
        "accent": c.THUMB_ACCENT,
        "text": c.THUMB_TEXT,
        "bg_style": DEFAULT_BG_STYLE,
        "focal": DEFAULT_FOCAL_STYLE,
    }]


def _pick_palette(c, title):
    """Deterministic palette choice from the title.

    sha256 rather than hash(): PYTHONHASHSEED randomises the builtin per
    process, so hash() would give a different thumbnail on every run and break
    the "same title -> same image" guarantee."""
    palettes = _palettes(c)
    digest = hashlib.sha256((title or "").strip().lower().encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(palettes)
    pal = dict(palettes[idx])
    pal.setdefault("bg2", pal["bg"])
    pal.setdefault("bg_style", DEFAULT_BG_STYLE)
    pal.setdefault("focal", DEFAULT_FOCAL_STYLE)
    pal["_rgb"] = {k: _hex_to_rgb(pal[k]) for k in ("bg", "bg2", "accent", "text")}
    return pal


# --- fonts -------------------------------------------------------------------


def _font(size):
    candidates = [
        config.assets_dir / "fonts" / getattr(config.channel, "THUMB_FONT",
                                              "Anton-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(str(c), size)
        except Exception:
            continue
    log.warning("no TTF font found; thumbnail text will use PIL default (small)")
    return ImageFont.load_default()


# --- background --------------------------------------------------------------


def _gradient_background(pal):
    """Two-stop gradient between the palette's bg and bg2, vertical or
    diagonal. Built small and upscaled - a 160x90 interpolation resized with
    BICUBIC is smooth at 1280x720 and avoids a per-pixel loop over 921k
    pixels."""
    top, bottom = pal["_rgb"]["bg"], pal["_rgb"]["bg2"]
    diagonal = pal["bg_style"] == "diagonal"
    sw, sh = 160, 90
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    for y in range(sh):
        fy = y / (sh - 1)
        for x in range(sw):
            t = (fy + x / (sw - 1)) / 2 if diagonal else fy
            px[x, y] = _mix(top, bottom, t)
    return small.resize((W, H), Image.BICUBIC)


# --- geometry helpers --------------------------------------------------------


def _rects_overlap(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


# --- copy analysis -----------------------------------------------------------


def _extract_number(*texts):
    """The first featureable number, formatting preserved ("$62,000", "3", not
    "62000"), plus the exact substring it came from so the caller can strip it
    out of the headline. Returns (number, source_text, span) or (None, ...)."""
    for text in texts:
        for m in _NUMBER_RE.finditer(text or ""):
            token = m.group().strip()
            if token and len(token) <= MAX_FOCAL_CHARS:
                return token, text, m.span()
    return None, None, None


def _keywords(*texts):
    """2-4 keyword words for the no-number focal, in their original order.

    Tries each text in turn and takes the first that yields at least
    MIN_KEYWORDS content words; if none does, falls back to the raw words of
    the first non-empty text so there is always something to draw."""
    fallback = []
    for text in texts:
        words = [w for w in re.split(r"[^\w'$%-]+", text or "") if w]
        if not words:
            continue
        fallback = fallback or words[:MAX_KEYWORDS]
        content = [w for w in words if w.lower() not in STOPWORDS]
        if len(content) >= MIN_KEYWORDS:
            return content[:MAX_KEYWORDS]
    return fallback


WARNING_WORDS = ("stop", "avoid", "trap", "mistake", "don't", "dont", "never")
GROWTH_WORDS = ("grow", "gain", "win", "rich", "build")


def _icon_kind(*texts):
    """'warning', 'growth', or None (no badge) based on simple keyword
    matching - deliberately conservative: no match means no badge rather
    than forcing an unrelated icon onto the thumbnail."""
    combined = " ".join(t or "" for t in texts).lower()
    if any(w in combined for w in WARNING_WORDS):
        return "warning"
    if any(w in combined for w in GROWTH_WORDS):
        return "growth"
    return None


# --- text blocks -------------------------------------------------------------


def _wrap_lines(draw, text, font, max_width):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def _fit_lines(draw, text, base_size, max_width, max_lines):
    """Shrink the font until text wraps within max_lines and max_width."""
    size = base_size
    while size > 40:
        font = _font(size)
        lines = _wrap_lines(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return lines, font
        size -= 8
    return lines[:max_lines], font


def _draw_text_block(draw, text, x, y, font_size, max_width, max_lines,
                     fill, stroke_fill, stroke_width):
    lines, font = _fit_lines(draw, text, font_size, max_width, max_lines)
    cur_y = y
    max_x1 = 0
    for line in lines:
        bbox = draw.textbbox((x, cur_y), line, font=font, stroke_width=stroke_width)
        draw.text((x, cur_y), line, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        max_x1 = max(max_x1, bbox[2])
        cur_y = bbox[3] + 12
    return (x, y, max_x1, cur_y)


# --- badges ------------------------------------------------------------------


def _draw_growth_icon(draw, cx, cy, radius, accent_rgb, bg_rgb):
    """Filled accent circle with a heavy upward arrow - grow/gain/win/rich/build."""
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=accent_rgb)
    aw = radius * 0.6   # arrow half-width
    ah = radius * 0.75  # arrow half-height
    draw.polygon([
        (cx, cy - ah), (cx + aw, cy + ah * 0.15), (cx + aw * 0.4, cy + ah * 0.15),
        (cx + aw * 0.4, cy + ah), (cx - aw * 0.4, cy + ah),
        (cx - aw * 0.4, cy + ah * 0.15), (cx - aw, cy + ah * 0.15),
    ], fill=bg_rgb)


def _draw_warning_icon(draw, cx, cy, radius, accent_rgb, bg_rgb):
    """Accent stop-octagon with a heavy X - stop/avoid/trap/mistake/don't."""
    n = 8
    pts = [(cx + radius * math.sin(math.pi / 8 + i * (2 * math.pi / n)),
           cy - radius * math.cos(math.pi / 8 + i * (2 * math.pi / n)))
           for i in range(n)]
    draw.polygon(pts, fill=accent_rgb)
    t = radius * 0.42
    lw = max(4, int(radius * 0.22))
    draw.line([(cx - t, cy - t), (cx + t, cy + t)], fill=bg_rgb, width=lw)
    draw.line([(cx - t, cy + t), (cx + t, cy - t)], fill=bg_rgb, width=lw)


# --- per-palette focal treatment ---------------------------------------------


def _draw_styled_text(draw, text, x, y, font, pal):
    """Draw one run of display text in the palette's focal treatment. All four
    styles stay high-contrast against the palette's own background, which is
    what keeps them legible at feed size."""
    accent = pal["_rgb"]["accent"]
    bg = pal["_rgb"]["bg2"]
    text_rgb = pal["_rgb"]["text"]
    style = pal["focal"]

    if style == "outline":
        # Hollow: background-coloured fill, thick accent stroke.
        draw.text((x, y), text, font=font, fill=bg,
                  stroke_width=10, stroke_fill=accent)
    elif style == "slab":
        # Accent block behind, background-coloured text knocked out of it.
        bbox = draw.textbbox((x, y), text, font=font)
        pad_x, pad_y = 26, 14
        draw.rectangle([bbox[0] - pad_x, bbox[1] - pad_y,
                        bbox[2] + pad_x, bbox[3] + pad_y], fill=accent)
        draw.text((x, y), text, font=font, fill=bg)
    elif style == "shadow":
        # Hard offset accent shadow under text-coloured type.
        off = max(6, font.size // 14)
        draw.text((x + off, y + off), text, font=font, fill=accent)
        draw.text((x, y), text, font=font, fill=text_rgb,
                  stroke_width=4, stroke_fill=bg)
    else:  # "solid"
        draw.text((x, y), text, font=font, fill=accent,
                  stroke_width=8, stroke_fill=bg)


def _styled_bbox(draw, text, x, y, font, style):
    """Bounding box _draw_styled_text will actually cover, stroke/slab/shadow
    padding included - needed for the reserved-zone check."""
    if style == "outline":
        return draw.textbbox((x, y), text, font=font, stroke_width=10)
    if style == "slab":
        b = draw.textbbox((x, y), text, font=font)
        return (b[0] - 26, b[1] - 14, b[2] + 26, b[3] + 14)
    if style == "shadow":
        off = max(6, font.size // 14)
        b = draw.textbbox((x, y), text, font=font, stroke_width=4)
        return (b[0], b[1], b[2] + off, b[3] + off)
    return draw.textbbox((x, y), text, font=font, stroke_width=8)


def _fit_single_line(draw, text, base_size, max_w, max_h, min_size=64):
    """Largest font at which `text` fits on one line inside max_w x max_h."""
    size = base_size
    while size > min_size:
        font = _font(size)
        b = draw.textbbox((0, 0), text, font=font, stroke_width=10)
        if (b[2] - b[0]) <= max_w and (b[3] - b[1]) <= max_h:
            return font
        size -= 12
    return _font(min_size)


def _draw_focal_number(draw, number, cx, cy, pal):
    """Big number centred on (cx, cy), in the palette's treatment. Returns the
    bbox actually covered."""
    font = _fit_single_line(draw, number, 420, int(W * 0.44), int(H * 0.55),
                            min_size=120)
    b = draw.textbbox((0, 0), number, font=font, stroke_width=8)
    tw, th = b[2] - b[0], b[3] - b[1]
    x = cx - tw / 2 - b[0]
    y = cy - th / 2 - b[1]

    bbox = _styled_bbox(draw, number, x, y, font, pal["focal"])
    if _rects_overlap(bbox, RESERVED_ZONE):
        shift = bbox[3] - RESERVED_ZONE[1] + 24
        y -= shift
        bbox = (bbox[0], bbox[1] - shift, bbox[2], bbox[3] - shift)

    _draw_styled_text(draw, number, x, y, font, pal)
    return bbox


def _draw_keyword_stack(draw, words, pal, x, max_w, top, bottom):
    """The no-number focal: 2-4 keyword words stacked, each set as large as it
    can go on its own line. The per-word sizing is what gives the stack its
    kinetic, hand-set look instead of reading like a wrapped sentence.

    'solid' alternates text/accent colour per word, since drawing every word in
    the accent would flatten the hierarchy; the other three styles apply their
    treatment to every word."""
    gap = 16
    n = max(1, len(words))
    max_line_h = (bottom - top - gap * (n - 1)) / n

    fonts = [_fit_single_line(draw, w.upper(), 210, max_w, max_line_h, min_size=56)
             for w in words]
    heights = [draw.textbbox((0, 0), w.upper(), font=f, stroke_width=10)[3]
               - draw.textbbox((0, 0), w.upper(), font=f, stroke_width=10)[1]
               for w, f in zip(words, fonts)]
    total = sum(heights) + gap * (n - 1)
    cur_y = top + max(0, (bottom - top - total) / 2)

    covered = None
    for i, (word, font, hgt) in enumerate(zip(words, fonts, heights)):
        text = word.upper()
        b = draw.textbbox((0, 0), text, font=font, stroke_width=10)
        y = cur_y - b[1]
        if pal["focal"] == "solid":
            # Alternate so the stack has a rhythm rather than one flat colour.
            colour = pal["_rgb"]["accent"] if i % 2 else pal["_rgb"]["text"]
            draw.text((x - b[0], y), text, font=font, fill=colour,
                      stroke_width=8, stroke_fill=pal["_rgb"]["bg2"])
            box = draw.textbbox((x - b[0], y), text, font=font, stroke_width=8)
        else:
            _draw_styled_text(draw, text, x - b[0], y, font, pal)
            box = _styled_bbox(draw, text, x - b[0], y, font, pal["focal"])
        covered = box if covered is None else (
            min(covered[0], box[0]), min(covered[1], box[1]),
            max(covered[2], box[2]), max(covered[3], box[3]))
        cur_y += hgt + gap
    return covered


# --- composition -------------------------------------------------------------


def _compose(script):
    """Build the thumbnail image for a script. Pure Pillow, no I/O, so it can
    be exercised directly for previews."""
    c = config.channel
    title = script.get("title", "")
    pal = _pick_palette(c, title)
    accent = pal["_rgb"]["accent"]
    text_rgb = pal["_rgb"]["text"]

    img = _gradient_background(pal)
    draw = ImageDraw.Draw(img)

    thumb_text = script.get("thumbnail_text") or ""
    number, src_text, span = _extract_number(thumb_text, title)
    badge = _icon_kind(thumb_text, title)

    if number:
        # --- headline (upper-left) + giant number (right-of-centre) ---
        # Strip the number from the headline using the exact span it matched,
        # so "$62,000" disappears rather than surviving as "$,000".
        headline = thumb_text or title
        if src_text == headline:
            headline = (headline[:span[0]] + headline[span[1]:])
        headline = re.sub(r"\s{2,}", " ", headline).strip(" -:,").strip()
        if not headline:
            headline = " ".join(_keywords(title))

        focal_box = _draw_focal_number(draw, number, int(W * 0.74), int(H * 0.46), pal)
        text_box = _draw_text_block(
            draw, headline.upper(), x=70, y=90, font_size=112,
            max_width=int(W * 0.48), max_lines=3,
            fill=text_rgb, stroke_fill=(0, 0, 0), stroke_width=6,
        )
        focal_desc = f"number:{number}"
    else:
        # --- no strong number: keyword stack IS the focal ---
        words = _keywords(thumb_text, title)
        radius = int(H * 0.20)
        badge_cx, badge_cy = int(W * 0.80), int(H * 0.42)
        max_w = int(W * (0.54 if badge else 0.80))

        if badge:
            if _rects_overlap((badge_cx - radius, badge_cy - radius,
                               badge_cx + radius, badge_cy + radius), RESERVED_ZONE):
                badge_cy -= radius
            drawer = _draw_warning_icon if badge == "warning" else _draw_growth_icon
            drawer(draw, badge_cx, badge_cy, radius, accent, pal["_rgb"]["bg2"])

        focal_box = _draw_keyword_stack(draw, words, pal, x=70, max_w=max_w,
                                        top=80, bottom=H - 130)
        text_box = focal_box
        focal_desc = f"keywords:{'/'.join(words)}" + (f"+{badge}" if badge else "")

    for name, box in (("focal", focal_box), ("headline", text_box)):
        if box and _rects_overlap(box, RESERVED_ZONE):
            log.warning("thumbnail %s bbox %s overlaps YouTube's reserved zone %s",
                        name, box, RESERVED_ZONE)

    # Accent bar (left edge) + brand wordmark (bottom-left, well clear of the
    # bottom-right reserved zone).
    draw.rectangle([0, 0, 14, H], fill=accent)
    draw.text((40, H - 70), c.NAME, font=_font(40), fill=accent)

    return img.convert("RGB"), pal, focal_desc


def make_thumbnail(run_id, script, force=False):
    out = stage_path(run_id, OUTPUT_NAME)
    if out.exists() and not force:
        log.info("using cached thumbnail")
        return str(out)

    img, pal, focal_desc = _compose(script)
    img.save(out, "JPEG", quality=90)
    log.info("thumbnail ok: %s (palette=%s, focal=%s)",
             out, pal["name"], focal_desc)
    return str(out)

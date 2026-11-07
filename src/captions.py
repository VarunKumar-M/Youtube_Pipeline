"""Stage 4 (render) - build caption image clips from word timings.

We already have exact word timings from voice.py. Here we group words into
short on-screen phrases (a few words each) and render each phrase to a
transparent PNG with Pillow, so MoviePy can overlay them WITHOUT needing
ImageMagick (MoviePy's TextClip needs ImageMagick, which is a common source of
breakage; PIL avoids it entirely).

Output:
  captions.json   [{text, start, end, png}]  + PNG files under captions/
"""
import json

from PIL import Image, ImageDraw, ImageFont

from .config import config
from .util import get_logger, stage_path, run_dir, load_json

log = get_logger("captions")

CACHE_NAME = "captions.json"
WORDS_PER_CUE = 4          # how many words show on screen at once (landscape)
FONT_SIZE = 64
STROKE = 4                 # black outline width for readability over video
MARGIN = 48                # keep caption text off the frame edges

# Shorts are 9:16 portrait - much narrower than the 16:9 landscape frame, so
# they need fewer words per line and a smaller font to avoid edge cutoff.
SHORT_WORDS_PER_CUE = 2
SHORT_FONT_SIZE = 56


def _load_font(size):
    """Use a bundled font if present, else a common system font, else default."""
    candidates = [
        config.assets_dir / "fonts" / "caption.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(str(c), size)
        except Exception:
            continue
    log.warning("no TTF font found; captions will use PIL default (small)")
    return ImageFont.load_default()


def _group(timings, words_per_cue):
    """Group word timings into cues of words_per_cue words."""
    cues = []
    for i in range(0, len(timings), words_per_cue):
        chunk = timings[i:i + words_per_cue]
        text = " ".join(w["word"] for w in chunk).strip()
        if not text:
            continue
        cues.append({
            "text": text,
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        })
    return cues


def _fit_font(draw, text, base_size, max_width):
    """Shrink the font until text fits max_width, so a cue can never overflow
    past the frame edges regardless of word length or frame width."""
    size = base_size
    while size > 24:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=STROKE)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 4
    return font


def _render_png(text, path, width, font_size):
    img = Image.new("RGBA", (width, int(font_size * 2.2)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _fit_font(draw, text, font_size, width - 2 * MARGIN)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=STROKE)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2 - bbox[0]
    y = (img.height - th) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255),
              stroke_width=STROKE, stroke_fill=(0, 0, 0, 255))
    img.save(path)


def render_text_png(text, path, width, font_size=FONT_SIZE):
    """Render one arbitrary line of text with the exact caption styling (same
    font, stroke, MARGIN safe padding, and auto-shrink-to-fit) to a transparent
    PNG. Used for one-off on-screen lines that aren't word-timed cues - e.g.
    the Shorts end-of-clip line in shorts.py."""
    _render_png(text, path, width, font_size)
    return str(path)


def build_captions(run_id, timings, width=None, words_per_cue=None,
                    font_size=FONT_SIZE, cache_name=CACHE_NAME,
                    cap_dirname="captions", force=False):
    if stage_path(run_id, cache_name).exists() and not force:
        log.info("using cached captions (%s)", cache_name)
        return load_json(run_id, cache_name)

    width = width or config.settings.VIDEO_WIDTH
    words_per_cue = words_per_cue or WORDS_PER_CUE
    cap_dir = run_dir(run_id) / cap_dirname
    cap_dir.mkdir(exist_ok=True)

    cues = _group(timings, words_per_cue)
    for idx, cue in enumerate(cues):
        png = cap_dir / f"cue_{idx:04d}.png"
        _render_png(cue["text"], png, width, font_size)
        cue["png"] = str(png)

    stage_path(run_id, cache_name).write_text(
        json.dumps(cues, indent=2, ensure_ascii=False))
    log.info("captions ok: %d cues (%s)", len(cues), cache_name)
    return cues

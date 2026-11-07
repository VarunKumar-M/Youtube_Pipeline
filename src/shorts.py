"""Stage 7 - cut two vertical Shorts from the finished long-form narration.

Rebuilt natively in portrait from the cached scene clips (visuals.json) and
word timings, rather than cropped from the landscape video.mp4. Cropping the
finished 16:9 frame down to 9:16 also crops its landscape-width caption text
(centre-cropping to 1080 out of a resized ~3413px-wide frame keeps only ~32%
of the width), cutting captions off at the edges. Instead each window is
recomposed directly at the 1080x1920 frame size, with its own captions
rendered for that narrower width (fewer words per line, smaller font).

We do NOT re-render from scratch. We take two named body segments (see
script_gen's segments) as the Shorts, fit the same scene clips to 9:16, add
captions sized for the vertical frame, and write two <=60s clips. These
auto-publish (no gate) staggered after the long-form.

Window selection cuts on segment boundaries so a Short starts and ends
cleanly: the first body segment, and the longest body segment. Body segments
run 50-80s (script_gen), so most need trimming down to SHORT_LEN - that trim
lands on the nearest sentence boundary (via word timings) rather than an
arbitrary cut point.

The last END_CARD_SECONDS of every Short carries an on-screen END_CARD_TEXT
line (rendered text, not voiceover, so it survives muted autoplay). It uses the
same caption styling via captions.render_text_png and sits above the normal
caption band so the two never overlap.

Each window also records the source segment's title plus a one-line hook drawn
from it - upload.py builds the Short's own description (CTA + link back to the
long-form) from those, instead of reusing the long-form description.

Output:
  shorts.json  [{path, start, end, title, hook}]  + short_1.mp4, short_2.mp4
"""
import json

from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip,
    concatenate_videoclips,
)

from .config import config
from .util import get_logger, stage_path, run_dir, load_json
from .assembly import _scene_clip, _allocate, _background_music
from . import voice as voice_stage
from . import visuals as visuals_stage
from . import captions as captions_stage
from . import script_gen

log = get_logger("shorts")

CACHE_NAME = "shorts.json"
SHORT_LEN = 55.0          # seconds; stay safely under 60
SHORT_MIN_LEN = 20.0      # never trim a segment down to less than this
SHORT_CAPTIONS_CACHE = "captions_short.json"
SHORT_CAPTIONS_DIR = "captions_short"

# On-screen line over the tail of every Short (text, not voiceover, so it still
# reads under muted autoplay).
END_CARD_TEXT = "Full video on the channel"
END_CARD_SECONDS = 3.0
END_CARD_NAME = "end_card.png"
# Normal captions sit at 18% up from the bottom; the end card sits at 32% so it
# clears them entirely, and both stay well inside the mobile safe area (the
# Shorts UI overlays roughly the bottom 15%). Horizontal safe padding comes for
# free from captions.MARGIN + its shrink-to-fit.
END_CARD_Y_FRACTION = 0.32
CAPTION_Y_FRACTION = 0.18

HOOK_MAX_CHARS = 110      # past this a "hook" reads as a paragraph, not a line
SHORTS_PER_VIDEO = 2      # distinct body segments to cut Shorts from


def _clip_segment(seg, timings, total):
    """Return (start, end) for one segment, trimmed to <=SHORT_LEN on a
    sentence boundary (found in the segment's own text, see
    script_gen.sentence_boundary_time) if the segment itself runs longer."""
    start = min(seg["start_time"], total)
    end = min(seg["end_time"], total)
    if end - start <= SHORT_LEN:
        return start, end
    limit = start + SHORT_LEN
    cut = script_gen.sentence_boundary_time(seg, timings, limit, start + SHORT_MIN_LEN)
    return start, (cut if cut is not None else limit)


def _short_hook(seg):
    """One-line hook drawn from the Short's own topic, for its description.

    The window starts on the segment boundary, so the segment's first sentence
    IS the Short's opening line - the most honest hook available, and free
    (no extra model call). Falls back to the segment title when that sentence
    is too long to read as a single line."""
    first = next(iter(script_gen.split_sentences(seg.get("text") or "")), "")
    first = " ".join(first.split())
    if first and len(first) <= HOOK_MAX_CHARS:
        return first
    return (seg.get("title") or "").strip()


def _seg_len(seg):
    return seg["end_time"] - seg["start_time"]


def _pick_segments(body):
    """Up to SHORTS_PER_VIDEO DISTINCT body segments to cut Shorts from.

    If the script marked >=SHORTS_PER_VIDEO segments "short_ready" (written by
    the generator to stand alone as a complete mini-story with zero context -
    see script_gen_history.py, used only by the history channel today), those
    take priority: a Short is only as good as the segment it's cut from, and a
    segment authored to be self-contained beats one picked by a length/keyword
    heuristic. No channel's generic script_gen.py sets this field, so this
    branch is a no-op for them - falls straight through to the heuristic below.

    Otherwise: always the first body segment - it follows the hook, so it's
    the strongest opener - then the best of what's LEFT: the segment
    script_gen marked "reengage" (the mid-video beat it was told is "the one
    that actually moves the needle") when it's long enough to carry a Short,
    else the longest remaining.

    Selecting the second pick from the remainder is the whole point. The old
    rule took max() over the entire body list and appended it only `if longest
    is not body[0]` - so any video whose first segment happened to also be its
    longest produced ONE candidate and silently shipped one Short instead of
    two. That's a content-shaped bug: it fires or doesn't depending on how
    Gemini happened to balance the segment lengths that day."""
    short_ready = [s for s in body if s.get("short_ready")]
    if len(short_ready) >= SHORTS_PER_VIDEO:
        return short_ready[:SHORTS_PER_VIDEO]

    picked = [body[0]]
    rest = list(body[1:])
    while rest and len(picked) < SHORTS_PER_VIDEO:
        strong = [s for s in rest if s.get("reengage") and _seg_len(s) >= SHORT_MIN_LEN]
        pick = strong[0] if strong else max(rest, key=_seg_len)
        picked.append(pick)
        rest.remove(pick)
    if len(picked) < SHORTS_PER_VIDEO:
        log.warning("only %d body segment(s) in this script; cutting %d Short(s) "
                    "instead of %d", len(body), len(picked), SHORTS_PER_VIDEO)
    return picked


def _windows(script, timings, total):
    """Return up to SHORTS_PER_VIDEO windows as (start, end, segment), one per
    segment chosen by _pick_segments, each cut on its real segment boundary
    (trimmed to a sentence end if the segment runs past SHORT_LEN)."""
    resolved = script_gen.resolve_segment_times(script["segments"], timings)
    body = [s for s in resolved if s.get("kind") == "body"]
    if not body:
        return []

    candidates = _pick_segments(body)

    wins = []
    for seg in candidates:
        start, end = _clip_segment(seg, timings, total)
        trimmed = (seg["end_time"] - seg["start_time"]) > SHORT_LEN
        log.info("short from segment '%s': %.0fs-%.0fs%s",
                seg["title"], start, end, " (trimmed to sentence end)" if trimmed else "")
        wins.append((start, end, seg))
    return wins


def _window_captions(cues, start, end):
    """Cues overlapping [start, end), re-timed relative to the window start."""
    out = []
    for cue in cues:
        if cue["end"] <= start or cue["start"] >= end:
            continue
        out.append({
            **cue,
            "start": max(0.0, cue["start"] - start),
            "end": min(end - start, cue["end"] - start),
        })
    return out


def make_shorts(run_id, script, force=False):
    if stage_path(run_id, CACHE_NAME).exists() and not force:
        log.info("using cached shorts")
        return load_json(run_id, CACHE_NAME)

    w, h = config.settings.SHORT_WIDTH, config.settings.SHORT_HEIGHT

    v = voice_stage.generate_voice(run_id, script)
    voice_audio = AudioFileClip(str(stage_path(run_id, "voice.mp3")))
    total = voice_audio.duration

    vis = visuals_stage.fetch_visuals(run_id, script)
    by_index = {e["scene_index"]: e for e in vis}

    cues = captions_stage.build_captions(
        run_id, v["timings"], width=w,
        words_per_cue=captions_stage.SHORT_WORDS_PER_CUE,
        font_size=captions_stage.SHORT_FONT_SIZE,
        cache_name=SHORT_CAPTIONS_CACHE, cap_dirname=SHORT_CAPTIONS_DIR,
        force=force,
    )

    scenes = script["scenes"]
    durations = _allocate(scenes, total)
    scene_clips = [_scene_clip(by_index.get(i, {"path": None}), durations[i], w, h)
                   for i in range(len(scenes))]
    base = concatenate_videoclips(scene_clips, method="compose").set_duration(total)

    music = _background_music(total)
    y = h - int(h * CAPTION_Y_FRACTION)
    end_card_y = h - int(h * END_CARD_Y_FRACTION)

    # One shared PNG for every Short - same text, same styling as the captions.
    end_card_png = captions_stage.render_text_png(
        END_CARD_TEXT, run_dir(run_id) / END_CARD_NAME, w,
        font_size=captions_stage.SHORT_FONT_SIZE)

    results = []
    for n, (start, end, seg) in enumerate(_windows(script, v["timings"], total), start=1):
        sub = base.subclip(start, end)
        length = end - start
        win_cues = _window_captions(cues, start, end)
        cap_clips = [
            (ImageClip(c["png"], transparent=True)
             .set_start(c["start"])
             .set_duration(max(0.4, c["end"] - c["start"]))
             .set_position(("center", y)))
            for c in win_cues
        ]
        card_len = min(END_CARD_SECONDS, length)
        end_card = (ImageClip(end_card_png, transparent=True)
                    .set_start(length - card_len)
                    .set_duration(card_len)
                    .set_position(("center", end_card_y)))
        composite = CompositeVideoClip([sub, *cap_clips, end_card], size=(w, h))

        audio_parts = [voice_audio.subclip(start, end)]
        if music:
            audio_parts.append(music.subclip(start, end))
        audio = (CompositeAudioClip(audio_parts) if len(audio_parts) > 1
                 else audio_parts[0])
        composite = composite.set_audio(audio).set_duration(end - start)

        out = run_dir(run_id) / f"short_{n}.mp4"
        log.info("rendering short %d (%.0fs-%.0fs), end card %.1fs-%.1fs...",
                 n, start, end, length - card_len, length)
        composite.write_videofile(str(out), fps=config.settings.FPS,
                                  codec="libx264", audio_codec="aac",
                                  threads=4, preset="medium", logger=None)
        results.append({"path": str(out), "start": start, "end": end,
                        "title": seg.get("title", ""), "hook": _short_hook(seg)})
        # Do NOT composite.close() here: `sub`/`audio_parts[0]` are shallow
        # copies of `base`/`voice_audio` (subclip() shares the same reader,
        # not a fresh one - see moviepy's Clip.close() docstring: "closing a
        # Clip may affect its copies"). Closing per-iteration was closing the
        # shared ffmpeg reader out from under the NEXT short, crashing it
        # with "'NoneType' object has no attribute 'stdout'". base/voice_audio
        # are already closed exactly once below, after every short is done.

    base.close()
    voice_audio.close()
    stage_path(run_id, CACHE_NAME).write_text(
        json.dumps(results, indent=2, ensure_ascii=False))
    log.info("shorts ok: %d clips", len(results))
    return results

"""Stage 6 - assemble the final long-form video with MoviePy.

Pins: moviepy==1.0.3, Pillow<10 (1.0.3's resize calls Image.ANTIALIAS, removed
in Pillow 10). imageio-ffmpeg (pulled in by moviepy) bundles a static ffmpeg, so
a separate system ffmpeg install is usually not required.

Pipeline:
  audio duration  -> allocate each scene a slice (weighted by its narration length)
  per scene       -> stock clip fitted to frame, looped/trimmed to its slice
  concat scenes   -> base video
  overlay caption PNGs at their timings (lower third)
  set narration as audio, optional low background music
  write video.mp4
"""
from pathlib import Path

from moviepy.editor import (
    VideoFileClip, ImageClip, ColorClip, AudioFileClip,
    CompositeVideoClip, CompositeAudioClip, concatenate_videoclips,
)
from moviepy.video.fx.loop import loop as fx_loop

from .config import config
from .util import get_logger, stage_path

log = get_logger("assembly")

MIN_SCENE = 1.5           # seconds; keep scenes from being unwatchably short
MUSIC_VOLUME = 0.07
OUTPUT_NAME = "video.mp4"


def _fit(clip, w, h):
    """Resize+centre-crop so the clip fills the frame without distortion."""
    clip_ratio = clip.w / clip.h
    target_ratio = w / h
    if clip_ratio > target_ratio:
        clip = clip.resize(height=h)
    else:
        clip = clip.resize(width=w)
    return clip.crop(x_center=clip.w / 2, y_center=clip.h / 2, width=w, height=h)


def _scene_clip(entry, duration, w, h):
    path = entry.get("path")
    if not path or not Path(path).exists():
        return ColorClip(size=(w, h), color=(15, 18, 28)).set_duration(duration)
    src = VideoFileClip(path).without_audio()
    src = _fit(src, w, h)
    if src.duration >= duration:
        return src.subclip(0, duration)
    looped = src.fx(fx_loop, duration=duration)
    return looped.subclip(0, duration)


def _allocate(scenes, total):
    weights = [max(1, len((s.get("text") or "").split())) for s in scenes]
    tw = sum(weights)
    durs = [max(MIN_SCENE, total * w / tw) for w in weights]
    # Normalise so the sum matches audio length exactly.
    scale = total / sum(durs)
    return [d * scale for d in durs]


def _background_music(total):
    music_dir = config.assets_dir / "music"
    tracks = sorted(list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.m4a")))
    if not tracks:
        return None
    m = AudioFileClip(str(tracks[0]))
    if m.duration < total:
        m = m.fx(fx_loop, duration=total)
    return m.subclip(0, total).volumex(MUSIC_VOLUME)


def assemble(run_id, script, visuals, captions, short=False, force=False):
    out = stage_path(run_id, OUTPUT_NAME)
    if out.exists() and not force:
        log.info("using cached video")
        return str(out)

    w, h = config.settings.VIDEO_WIDTH, config.settings.VIDEO_HEIGHT
    voice = AudioFileClip(str(stage_path(run_id, "voice.mp3")))
    total = voice.duration

    scenes = script["scenes"]
    by_index = {v["scene_index"]: v for v in visuals}
    durations = _allocate(scenes, total)

    log.info("assembling %d scenes over %.1fs", len(scenes), total)
    clips = []
    for i, scene in enumerate(scenes):
        entry = by_index.get(i, {"path": None})
        clips.append(_scene_clip(entry, durations[i], w, h))

    base = concatenate_videoclips(clips, method="compose").set_duration(total)

    caption_clips = []
    y = h - int(h * 0.18)
    for cue in captions:
        ic = (ImageClip(cue["png"], transparent=True)
              .set_start(cue["start"])
              .set_duration(max(0.4, cue["end"] - cue["start"]))
              .set_position(("center", y)))
        caption_clips.append(ic)

    video = CompositeVideoClip([base, *caption_clips], size=(w, h))

    music = _background_music(total)
    audio = CompositeAudioClip([voice, music]) if music else voice
    video = video.set_audio(audio).set_duration(total)

    log.info("rendering %s (this is the slow step)...", OUTPUT_NAME)
    video.write_videofile(
        str(out),
        fps=config.settings.FPS,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        preset="medium",
        logger=None,
    )
    for c in clips:
        c.close()
    voice.close()
    log.info("video ok: %s", out)
    return str(out)

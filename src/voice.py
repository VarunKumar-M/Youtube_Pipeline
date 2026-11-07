"""Stage 3 + 4 - narration audio AND word-level caption timings in one pass.

edge-tts emits WordBoundary events alongside the audio stream, giving exact
offsets/durations for every spoken word. So we do NOT need Whisper: no model
download, no CPU transcription, and timings are exact rather than inferred.

Outputs:
  voice.mp3      narration audio
  timings.json   [{word, start, end}] in seconds
"""
import asyncio
import json

import edge_tts

from .config import config
from .util import get_logger, retry, stage_path, load_json

log = get_logger("voice")

AUDIO_NAME = "voice.mp3"
TIMINGS_NAME = "timings.json"

# WordBoundary offsets/durations are in 100-nanosecond ticks.
TICKS_PER_SECOND = 10_000_000


def pick_voice(run_id):
    """Deterministic per-run rotation (within the channel's own voice pool)
    so a re-render keeps the same voice."""
    voices = config.channel.VOICES
    return voices[sum(ord(c) for c in run_id) % len(voices)]


async def _synth(text, voice, audio_path):
    communicate = edge_tts.Communicate(text, voice, rate="+5%", boundary="WordBoundary")
    words = []
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_SECOND
                dur = chunk["duration"] / TICKS_PER_SECOND
                words.append({
                    "word": chunk["text"],
                    "start": round(start, 3),
                    "end": round(start + dur, 3),
                })
    return words


@retry(times=3, base_delay=4.0)
def _run_synth(text, voice, audio_path):
    return asyncio.run(_synth(text, voice, audio_path))


def generate_voice(run_id, script, force=False):
    audio_path = stage_path(run_id, AUDIO_NAME)
    timings_path = stage_path(run_id, TIMINGS_NAME)

    if audio_path.exists() and timings_path.exists() and not force:
        log.info("using cached voice")
        return {"audio": str(audio_path), "timings": load_json(run_id, TIMINGS_NAME)}

    voice = pick_voice(run_id)
    log.info("synthesising narration with %s", voice)

    words = _run_synth(script["narration"], voice, audio_path)
    if not words:
        raise RuntimeError("edge-tts returned no word timings")

    timings_path.write_text(json.dumps(words, indent=2, ensure_ascii=False))

    duration = words[-1]["end"]
    size_mb = audio_path.stat().st_size / 1_048_576
    log.info("voice ok: %.1fs, %.1f MB, %d words", duration, size_mb, len(words))

    return {"audio": str(audio_path), "timings": words}


def audio_duration(timings):
    return timings[-1]["end"] if timings else 0.0

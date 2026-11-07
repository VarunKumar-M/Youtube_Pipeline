"""Shared chunked-download + validation logic for anything that fetches a
video file to disk. Extracted out of visuals.py so the archive-footage backend
(src/archive_visuals.py) reuses the exact same truncation-safe path instead of
duplicating it - archive files run much larger than stock clips, so a short
write here is even more expensive to get wrong, not less.

EVERY download is validated before it is accepted. A socket that drops
mid-stream leaves a partial file that still *opens*: moviepy then logs "N
bytes wanted but 0 bytes read ... Using the last valid frame instead" and
bakes the broken clip into the render, which YouTube eventually rejects as
"Processing abandoned". So a short write is treated as a hard failure - the
partial file is deleted and the caller decides whether to retry.
"""
import subprocess

import imageio_ffmpeg
import requests

from .util import get_logger, retry

log = get_logger("media_download")

# A validated clip has to clear both of these; anything smaller is a stub or
# an error page that happened to arrive with a 200.
MIN_CLIP_BYTES = 32 * 1024
MIN_CLIP_SECONDS = 0.2


class IncompleteDownload(RuntimeError):
    """Bytes on disk don't match what the server said it was sending, or the
    file won't decode. Retryable - the URL is usually fine, the socket wasn't."""


def stream_to_disk(url, path, timeout=120):
    """Chunked stream to disk. Returns (bytes_written, expected), where
    expected is the Content-Length as an int, or None if the server didn't
    send a usable one."""
    written = 0
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        expected = r.headers.get("Content-Length")
        # With Content-Encoding set, requests hands back DEcoded bytes while
        # Content-Length describes the ENcoded body - comparing the two would
        # fail every time. Drop to the decode probe instead.
        if r.headers.get("Content-Encoding"):
            expected = None
        try:
            expected = int(expected) if expected is not None else None
        except ValueError:
            expected = None
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                written += len(chunk)
    return written, expected


def _decode_check(path):
    """Decode the whole file with the bundled ffmpeg and fail on any error.

    This is what actually distinguishes a truncated clip from a whole one. The
    cheaper tricks don't: a cut-short mp4 usually keeps a valid header, so
    duration/frame-count still look right, and seeking to the last frame gives
    FALSE POSITIVES - ffmpeg's reported duration routinely runs a fraction past
    the last decodable frame, so healthy clips fail that seek too (measured: 8
    of 39 good clips). A full pass at -v error is unambiguous and costs ~0.15s
    for a 15s 720p clip, which is nothing next to re-downloading it."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run([exe, "-v", "error", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, timeout=180)
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or err:
        detail = err.splitlines()[0] if err else f"ffmpeg exit {proc.returncode}"
        raise IncompleteDownload(f"stream does not decode cleanly: {detail}")


def _probe_video(path):
    """Fallback check for servers that omit Content-Length: the file must open
    as a video, report a real duration and frame count, and then survive a full
    decode (see _decode_check)."""
    from moviepy.video.io.VideoFileClip import VideoFileClip

    try:
        clip = VideoFileClip(str(path))
    except Exception as e:  # noqa: BLE001 - won't even open
        raise IncompleteDownload(f"not a readable video: {e}") from e
    try:
        duration = clip.duration or 0.0
        nframes = getattr(clip.reader, "nframes", 0) or 0
    finally:
        clip.close()
    if duration < MIN_CLIP_SECONDS or nframes <= 0:
        raise IncompleteDownload(
            f"opens but is unusable (duration={duration:.2f}s, frames={nframes})")
    _decode_check(path)
    return duration, nframes


def validate_download(path, written, expected):
    """Raise IncompleteDownload unless the file on disk is whole and playable.
    Returns a short string naming which check was used, for the log."""
    size = path.stat().st_size
    if size != written:
        raise IncompleteDownload(f"wrote {written} bytes but file is {size}")
    if size < MIN_CLIP_BYTES:
        raise IncompleteDownload(f"only {size} bytes - too small to be a clip")
    if expected is not None:
        if size != expected:
            raise IncompleteDownload(
                f"incomplete: got {size} of {expected} bytes "
                f"({100.0 * size / max(1, expected):.1f}%)")
        return f"content-length {expected}"
    duration, nframes = _probe_video(path)
    return f"no content-length; decoded {duration:.1f}s / {nframes} frames"


@retry(times=3, base_delay=3.0)
def download(url, path, timeout=120):
    """Download and validate one clip. retry() gives 1 attempt + 2 more, per
    URL. Any failure deletes the partial file first, so a later attempt (or a
    fallback provider writing to the same dest) never inherits half a clip and
    a failed scene never leaves a broken file lying around."""
    try:
        written, expected = stream_to_disk(url, path, timeout=timeout)
        how = validate_download(path, written, expected)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    log.debug("download ok (%s): %s", how, path.name)
    return path

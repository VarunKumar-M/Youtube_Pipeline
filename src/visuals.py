"""Stage 5 - fetch a video clip for each scene.

Backend selection is per-channel (config.channel.VISUALS_BACKEND): channels
that don't set it (the default) get the stock backend below, unchanged.
Setting VISUALS_BACKEND = "archive" routes the whole stage to
archive_visuals.fetch_visuals_archive() instead - see that module for the
Internet Archive / NASA licensed-footage backend used by the history channel.

--- stock backend (default) ---

Primary source: Pixabay video API (single key, no attribution required).
Fallback:      Pexels video API (if PEXELS_API_KEY is set) - also used when
               Pixabay's own rate limit is hit (any Pixabay failure, 429
               included, already falls through to Pexels below), AND when a
               Pixabay clip downloads but fails validation.

PEXELS_API_KEY is shared across channels (config.pexels_key), so its usage
counter is one shared file, not per-channel - Pexels enforces the limit per
key, not per caller.

EVERY download is validated before it is accepted (see media_download.py). A
socket that drops mid-stream leaves a partial .mp4 that still *opens*: moviepy
then logs "N bytes wanted but 0 bytes read ... Using the last valid frame
instead" and bakes the broken clip into the render, which YouTube eventually
rejects as "Processing abandoned". So a short write is treated as a hard
failure - the partial file is deleted, the same URL is retried, and if that
URL is still bad the OTHER provider is tried for the same query. A scene that
neither provider can supply raises VisualsError rather than silently becoming
a placeholder: a missing or truncated clip must never reach the render.

Output:
  visuals.json   [{scene_index, query, path, source}]  + mp4s under visuals/
"""
import json
import time

import requests

from .config import config
from .media_download import download as _download, IncompleteDownload  # noqa: F401
from .util import get_logger, retry, stage_path, run_dir, load_json

log = get_logger("visuals")

CACHE_NAME = "visuals.json"
PIXABAY_VIDEO = "https://pixabay.com/api/videos/"
PEXELS_VIDEO = "https://api.pexels.com/videos/search"

PEXELS_USAGE_FILE = config.root / ".pexels_usage.json"
PEXELS_HOURLY_LIMIT = 200
PEXELS_MONTHLY_LIMIT = 20_000
_SECONDS_PER_MONTH = 31 * 86400


class VisualsError(RuntimeError):
    """A scene could not be given a complete, playable clip by any provider."""


def _load_pexels_usage():
    try:
        return json.loads(PEXELS_USAGE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _record_pexels_call():
    now = time.time()
    calls = [t for t in _load_pexels_usage() if t > now - _SECONDS_PER_MONTH]
    calls.append(now)
    PEXELS_USAGE_FILE.write_text(json.dumps(calls))


def _pexels_quota_available():
    now = time.time()
    calls = _load_pexels_usage()
    hourly = sum(1 for t in calls if t > now - 3600)
    monthly = sum(1 for t in calls if t > now - _SECONDS_PER_MONTH)
    if hourly >= PEXELS_HOURLY_LIMIT:
        log.warning("pexels hourly limit reached (%d/%d); skipping pexels this call",
                   hourly, PEXELS_HOURLY_LIMIT)
        return False
    if monthly >= PEXELS_MONTHLY_LIMIT:
        log.warning("pexels monthly limit reached (%d/%d); skipping pexels this call",
                   monthly, PEXELS_MONTHLY_LIMIT)
        return False
    return True


@retry(times=3, base_delay=3.0)
def _pixabay(query):
    r = requests.get(PIXABAY_VIDEO, params={
        "key": config.pixabay_key,
        "q": query,
        "per_page": 5,
        "safesearch": "true",
        "video_type": "film",
    }, timeout=30)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    if not hits:
        return None
    files = hits[0].get("videos", {})
    # Prefer medium (720p) then large then small.
    for size in ("medium", "large", "small", "tiny"):
        if files.get(size, {}).get("url"):
            return files[size]["url"]
    return None


@retry(times=3, base_delay=3.0)
def _pexels(query):
    key = config.pexels_key
    if not key:
        return None
    if not _pexels_quota_available():
        return None
    r = requests.get(PEXELS_VIDEO,
                     headers={"Authorization": key},
                     params={"query": query, "per_page": 5,
                             "orientation": "landscape"},
                     timeout=30)
    _record_pexels_call()  # counts against Pexels' limit whether or not it hits
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        return None
    files = sorted(videos[0].get("video_files", []),
                   key=lambda f: f.get("width", 0))
    # Pick the smallest file that is at least 1280 wide, else the largest.
    for f in files:
        if f.get("width", 0) >= 1280:
            return f["link"]
    return files[-1]["link"] if files else None


# Ordered: Pixabay first, Pexels as the fallback for the same query. Each entry
# is tried end-to-end (search -> download -> validate) before the next.
PROVIDERS = (("pixabay", _pixabay), ("pexels", _pexels))


def _fetch_scene(index, query, dest):
    """Search, download and validate a clip for one scene, trying every
    provider in turn. Returns the provider name that worked.

    Raises VisualsError naming the scene and query if none of them can produce
    a complete, playable clip - deliberately fatal, see the module docstring."""
    problems = []
    for name, search in PROVIDERS:
        try:
            url = search(query)          # already @retry-wrapped
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name} search failed: {e}")
            continue
        if not url:
            problems.append(f"{name}: no results")
            continue
        try:
            _download(url, dest)
            return name
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name} download failed ({url}): {e}")
            log.warning("scene %d: %s clip unusable, trying next provider: %s",
                        index, name, e)
    raise VisualsError(
        f"scene {index} ('{query}'): no provider could supply a complete, "
        f"playable clip after retries. Tried: " + "; ".join(problems)
    )


def fetch_visuals(run_id, script, force=False):
    # Channels that don't set VISUALS_BACKEND keep this stock behaviour
    # unchanged. Import deferred to avoid a module-load cycle (archive_visuals
    # imports VisualsError from this module).
    backend = getattr(config.channel, "VISUALS_BACKEND", "stock")
    if backend == "archive":
        from . import archive_visuals
        return archive_visuals.fetch_visuals_archive(run_id, script, force=force)
    if backend != "stock":
        raise VisualsError(f"unknown VISUALS_BACKEND '{backend}' for channel "
                           f"'{config.channel_slug}'")

    if stage_path(run_id, CACHE_NAME).exists() and not force:
        log.info("using cached visuals")
        return load_json(run_id, CACHE_NAME)

    vdir = run_dir(run_id) / "visuals"
    vdir.mkdir(exist_ok=True)
    results = []

    n = len(script["scenes"])
    for i, scene in enumerate(script["scenes"]):
        query = scene.get("visual_query", "").strip() or "abstract background"
        dest = vdir / f"scene_{i:04d}.mp4"
        source = _fetch_scene(i, query, dest)   # raises VisualsError if nothing works
        results.append({"scene_index": i, "query": query,
                        "path": str(dest), "source": source})
        log.info("scene %d/%d: '%s' -> %s", i + 1, n, query, source)

    # Only written once every scene has a validated clip, so a cached
    # visuals.json can never describe a partially-fetched run.
    stage_path(run_id, CACHE_NAME).write_text(
        json.dumps(results, indent=2, ensure_ascii=False))
    log.info("visuals ok: %d/%d clips fetched and validated", len(results), n)
    return results

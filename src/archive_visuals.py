"""Stage 5 alt backend - fetch provably-licensed archival footage per scene.

Selected per-channel via config.channel.VISUALS_BACKEND = "archive" (see
visuals.fetch_visuals, which dispatches here). Built for the history channel,
but the NASA source is general-purpose and reusable by a future space channel.

Sources:
  archive  - Internet Archive advancedsearch + metadata APIs. Gated by
             _decide_license(): only public domain / CC0 / CC-BY / CC-BY-SA
             items are ever downloaded. Archive.org's own guidance (e.g. the
             Prelinger collection notes) is not to assume permissive licensing
             for uncatalogued items, so this gate SKIPS anything it cannot
             positively confirm rather than guessing - see the module
             docstring on _decide_license for the exact rules.
  nasa     - images-api.nasa.gov. Everything returned is US-government public
             domain by law, so no license branching is needed here.

Per scene: try the channel's primary source, broadening the query (dropping
trailing words, then leading words - see _broadened_queries) up to twice each
way if nothing licensed turns up, then fall back to the other configured
source under the same broadening. Trailing-drop alone never helps a query
that front-loads a named individual (e.g. "margaret hamilton mit nasa
1960s" - dropping from the end never sheds the name), which is common enough
in practice that leading-drop is worth the extra attempts; the script
generator is also prompted to avoid such queries in the first place (see
script_gen_history.py). A scene neither source can supply raises VisualsError
naming the scene and query - never a placeholder, never an unlicensed clip
(see visuals.py's module docstring for why a truncated/missing clip must
never reach the render; the same reasoning applies doubly to an unlicensed
one, since that risks a takedown or a false Content ID claim on the finished
video).

Evidence: every clip actually used is recorded in
output/<channel>/<run-id>/licenses.json (scene_index, query, source,
identifier, license, licenseurl, source_page_url, downloaded_file, and an
`attribution` string for CC-BY/CC-BY-SA clips) - this is what backs a dispute
if a false claim lands on the video. Written on every run, including
--dry-run, since fetch_visuals runs regardless of that flag.

NOTE for the upload step: `attribution` strings collected here are not yet
appended to the video description. The natural hook is
src/upload.py:_description() - have it read licenses.json for the run and
append any attribution lines after the AI disclosure line.

Output: visuals.json, same shape as the stock backend
  [{scene_index, query, path, source}]  + mp4s under visuals/
"""
import json

import requests

from .config import config
from .media_download import download
from .util import get_logger, retry, stage_path, run_dir, load_json
from .visuals import VisualsError

log = get_logger("archive_visuals")

CACHE_NAME = "visuals.json"

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{identifier}"
ARCHIVE_DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
ARCHIVE_DETAILS_URL = "https://archive.org/details/{identifier}"
ARCHIVE_ROWS = 25
ARCHIVE_PREFERRED_EXTS = (".mp4", ".ogv", ".mpeg")
# Scene clips need a few seconds of footage, not a multi-GB full reel, and
# archive.org has no API for a time-ranged partial download - so a candidate
# whose only files exceed this is skipped in favour of the next search result,
# same idea as the NASA "orig" master cap below.
ARCHIVE_MAX_FILE_BYTES = 500 * 1024 * 1024

NASA_SEARCH_URL = "https://images-api.nasa.gov/search"
NASA_PREFERRED_SUFFIXES = ("~large.mp4", "~medium.mp4", "~small.mp4")
NASA_MAX_ORIG_BYTES = 300 * 1024 * 1024  # cap: "orig" masters can run into GBs

MAX_BROADENINGS = 2
DOWNLOAD_TIMEOUT = 300  # archive files run much larger than stock clips


# --- Internet Archive --------------------------------------------------------

@retry(times=3, base_delay=3.0)
def _archive_search(query):
    r = requests.get(ARCHIVE_SEARCH_URL, params={
        "q": f"{query} AND mediatype:movies",
        "fl[]": ["identifier", "licenseurl", "title"],
        "rows": ARCHIVE_ROWS,
        "output": "json",
    }, timeout=30)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


@retry(times=3, base_delay=3.0)
def _archive_metadata(identifier):
    r = requests.get(ARCHIVE_METADATA_URL.format(identifier=identifier), timeout=30)
    r.raise_for_status()
    return r.json()


def _decide_license(metadata):
    """Return (licenseurl, label, reason). `label` is None (and `reason`
    explains why) unless the license is positively confirmed as one of
    public-domain / CC0 / CC-BY / CC-BY-SA. When in doubt, this returns None -
    never guess at a license from an absent or ambiguous field."""
    meta = metadata.get("metadata", {}) or {}
    licenseurl = (meta.get("licenseurl") or "").strip()
    url = licenseurl.lower()

    if url:
        if "by-nc" in url or "/nc/" in url or url.endswith("-nc"):
            return licenseurl, None, f"non-commercial license ({licenseurl})"
        if "by-nd" in url or "/nd/" in url or url.endswith("-nd"):
            return licenseurl, None, f"no-derivatives license ({licenseurl})"
        if "publicdomain" in url or "/zero/" in url:
            return licenseurl, "public-domain/CC0", None
        if "/by-sa" in url:
            return licenseurl, "CC-BY-SA", None
        if "/by/" in url:
            return licenseurl, "CC-BY", None
        return licenseurl, None, f"unrecognized/ambiguous licenseurl ({licenseurl})"

    # No licenseurl at all - only accept if free-text rights/license fields
    # unambiguously say public domain, with no NC/ND/"all rights reserved" hedge.
    rights_text = f"{meta.get('rights', '')} {meta.get('license', '')}".strip().lower()
    if not rights_text:
        return None, None, "no license field present"
    if any(bad in rights_text for bad in (
            "non-commercial", "noncommercial", "no derivative",
            "all rights reserved", "copyrighted", "copyright")):
        return None, None, f"rights text indicates restrictions ({rights_text!r})"
    if "public domain" in rights_text:
        return None, "public-domain (rights text)", None
    return None, None, f"unrecognized/ambiguous rights text ({rights_text!r})"


def _pick_archive_file(metadata):
    """Smallest-size-first within each preferred extension, and skip any file
    over ARCHIVE_MAX_FILE_BYTES - see the constant's comment. Returns
    (filename, reason) where filename is None if nothing qualified."""
    by_ext = {}
    oversized_only = False
    for f in metadata.get("files", []):
        name = f.get("name", "")
        lname = name.lower()
        for ext in ARCHIVE_PREFERRED_EXTS:
            if not lname.endswith(ext):
                continue
            try:
                size = int(f.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            if size > ARCHIVE_MAX_FILE_BYTES:
                oversized_only = True
                continue
            current = by_ext.get(ext)
            if current is None or size < current[1]:
                by_ext[ext] = (name, size)
    for ext in ARCHIVE_PREFERRED_EXTS:
        if ext in by_ext:
            return by_ext[ext][0], None
    if oversized_only:
        return None, f"only file(s) over the {ARCHIVE_MAX_FILE_BYTES} byte cap"
    return None, "no .mp4/.ogv/.mpeg file"


def _archive_creator(metadata):
    creator = metadata.get("metadata", {}).get("creator")
    if isinstance(creator, list):
        return ", ".join(creator)
    return creator


def _find_archive_clip(query):
    """Search archive.org for `query`, walk candidates in order, and return
    the first one that both clears the license gate and has a downloadable
    video file. Every candidate's license decision is logged - accepted or
    skipped and why - so the search itself is part of the dispute evidence,
    not just the item finally chosen."""
    try:
        docs = _archive_search(query)
    except Exception as e:  # noqa: BLE001
        log.warning("archive.org search failed for '%s': %s", query, e)
        return None

    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        try:
            metadata = _archive_metadata(identifier)
        except Exception as e:  # noqa: BLE001
            log.warning("archive.org metadata fetch failed for %s: %s", identifier, e)
            continue

        licenseurl, label, reason = _decide_license(metadata)
        if not label:
            log.info("archive candidate SKIPPED %s: %s", identifier, reason)
            continue

        filename, reason = _pick_archive_file(metadata)
        if not filename:
            log.info("archive candidate SKIPPED %s: license ok (%s) but %s",
                     identifier, label, reason)
            continue

        log.info("archive candidate ACCEPTED %s: license=%s file=%s",
                 identifier, label, filename)
        return {
            "source": "archive",
            "identifier": identifier,
            "license": label,
            "licenseurl": licenseurl or None,
            "source_page_url": ARCHIVE_DETAILS_URL.format(identifier=identifier),
            "download_url": ARCHIVE_DOWNLOAD_URL.format(identifier=identifier, filename=filename),
            "title": doc.get("title") or metadata.get("metadata", {}).get("title"),
            "creator": _archive_creator(metadata),
        }
    return None


# --- NASA ---------------------------------------------------------------------

@retry(times=3, base_delay=3.0)
def _nasa_search(query):
    r = requests.get(NASA_SEARCH_URL, params={"q": query, "media_type": "video"}, timeout=30)
    r.raise_for_status()
    return r.json().get("collection", {}).get("items", [])


@retry(times=3, base_delay=3.0)
def _nasa_manifest(href):
    r = requests.get(href, timeout=30)
    r.raise_for_status()
    return r.json()


def _nasa_head_bytes(url):
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except Exception:  # noqa: BLE001
        return None


def _pick_nasa_file(urls):
    mp4s = [u for u in urls if u.lower().endswith(".mp4")]
    if not mp4s:
        return None, "no .mp4 in asset manifest"
    for suffix in NASA_PREFERRED_SUFFIXES:
        for u in mp4s:
            if u.lower().endswith(suffix):
                return u, None
    # Nothing but the "orig" master left - only use it if it isn't huge.
    orig = next((u for u in mp4s if "orig" in u.lower()), mp4s[0])
    size = _nasa_head_bytes(orig)
    if size is not None and size > NASA_MAX_ORIG_BYTES:
        return None, f"only an 'orig' master and it's {size} bytes (cap {NASA_MAX_ORIG_BYTES})"
    return orig, None


def _find_nasa_clip(query):
    """Everything NASA's image/video library returns is US-government public
    domain by law, so there is no license gate here - only a file-size cap on
    the raw 'orig' masters, which can run into gigabytes."""
    try:
        items = _nasa_search(query)
    except Exception as e:  # noqa: BLE001
        log.warning("NASA search failed for '%s': %s", query, e)
        return None

    for item in items:
        href = item.get("href")
        data = (item.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id", "unknown")
        if not href:
            continue
        try:
            urls = _nasa_manifest(href)
        except Exception as e:  # noqa: BLE001
            log.warning("NASA manifest fetch failed for %s: %s", nasa_id, e)
            continue
        if not isinstance(urls, list):
            log.info("NASA candidate SKIPPED %s: manifest not a file list", nasa_id)
            continue

        file_url, reason = _pick_nasa_file(urls)
        if not file_url:
            log.info("NASA candidate SKIPPED %s: %s", nasa_id, reason)
            continue

        log.info("NASA candidate ACCEPTED %s: file=%s", nasa_id, file_url)
        return {
            "source": "nasa",
            "identifier": nasa_id,
            "license": "public-domain (US government work)",
            "licenseurl": None,
            "source_page_url": f"https://images.nasa.gov/details/{nasa_id}",
            "download_url": file_url,
            "title": data.get("title"),
            "creator": None,
        }
    return None


# --- fallback / broadening orchestration --------------------------------------

SOURCES = {"archive": _find_archive_clip, "nasa": _find_nasa_clip}


def _broadened_queries(query):
    """[query, minus trailing word(s), minus leading word(s)], each up to
    MAX_BROADENINGS deep, stopping once there's nothing left to drop.

    Trailing-drop alone never helps a query that front-loads a named
    individual ("margaret hamilton mit nasa 1960s" -> "margaret hamilton mit
    nasa" -> "margaret hamilton mit" - the name never moves), which is common
    enough that leading-drop is worth trying too before giving up on a scene."""
    words = query.split()
    queries = [query]
    for i in range(1, MAX_BROADENINGS + 1):
        if len(words) - i < 1:
            break
        candidate = " ".join(words[:-i])
        if candidate and candidate not in queries:
            queries.append(candidate)
    for i in range(1, MAX_BROADENINGS + 1):
        if len(words) - i < 1:
            break
        candidate = " ".join(words[i:])
        if candidate and candidate not in queries:
            queries.append(candidate)
    return queries


def _search_source(source_name, query):
    finder = SOURCES[source_name]
    for q in _broadened_queries(query):
        if q != query:
            log.info("%s: broadening query '%s' -> '%s'", source_name, query, q)
        result = finder(q)
        if result:
            result["query_used"] = q
            return result
    return None


def _fetch_scene(index, query, dest, primary_source, fallback_source):
    result = _search_source(primary_source, query)
    if not result and fallback_source and fallback_source != primary_source:
        log.info("scene %d: no licensed result from %s, falling back to %s",
                 index, primary_source, fallback_source)
        result = _search_source(fallback_source, query)

    if not result:
        sources = primary_source + (f" or {fallback_source}" if fallback_source else "")
        n_variants = len(_broadened_queries(query)) - 1
        raise VisualsError(
            f"scene {index} ('{query}'): no licensed clip found on {sources} "
            f"after {n_variants} query broadening attempt(s) (trailing- and "
            f"leading-word drops)."
        )

    try:
        download(result["download_url"], dest, timeout=DOWNLOAD_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise VisualsError(
            f"scene {index} ('{query}'): licensed candidate "
            f"{result['source']}:{result['identifier']} found, but its file "
            f"failed to download/validate: {e}"
        ) from e
    return result


def _license_record(index, query, dest, found):
    record = {
        "scene_index": index,
        "query": query,
        "source": found["source"],
        "identifier": found["identifier"],
        "license": found["license"],
        "licenseurl": found.get("licenseurl"),
        "source_page_url": found.get("source_page_url"),
        "downloaded_file": str(dest),
    }
    if found["license"] in ("CC-BY", "CC-BY-SA"):
        title = found.get("title") or found["identifier"]
        creator = found.get("creator") or "unknown creator"
        record["attribution"] = (
            f'"{title}" by {creator}, source: {found.get("source_page_url")}, '
            f'licensed under {found["license"]} ({found.get("licenseurl")})'
        )
    return record


def fetch_visuals_archive(run_id, script, force=False):
    if stage_path(run_id, CACHE_NAME).exists() and not force:
        log.info("using cached visuals")
        return load_json(run_id, CACHE_NAME)

    primary_source = getattr(config.channel, "VISUALS_PRIMARY_SOURCE", "archive")
    fallback_source = getattr(config.channel, "VISUALS_FALLBACK_SOURCE", None)
    if primary_source not in SOURCES:
        raise VisualsError(f"unknown VISUALS_PRIMARY_SOURCE '{primary_source}'")
    if fallback_source and fallback_source not in SOURCES:
        raise VisualsError(f"unknown VISUALS_FALLBACK_SOURCE '{fallback_source}'")

    vdir = run_dir(run_id) / "visuals"
    vdir.mkdir(exist_ok=True)
    results = []
    license_records = []

    n = len(script["scenes"])
    for i, scene in enumerate(script["scenes"]):
        query = scene.get("visual_query", "").strip() or "archival footage"
        dest = vdir / f"scene_{i:04d}.mp4"
        # Raises VisualsError naming the scene and query if nothing licensed
        # can be found - never a placeholder, never an unlicensed clip.
        found = _fetch_scene(i, query, dest, primary_source, fallback_source)
        results.append({"scene_index": i, "query": query,
                        "path": str(dest), "source": found["source"]})
        license_records.append(_license_record(i, query, dest, found))
        log.info("scene %d/%d: '%s' -> %s:%s (license=%s)",
                 i + 1, n, query, found["source"], found["identifier"], found["license"])

    # Dispute evidence - written every run, including --dry-run.
    licenses_path = run_dir(run_id) / "licenses.json"
    licenses_path.write_text(json.dumps(license_records, indent=2, ensure_ascii=False))
    log.info("license manifest written: %s (%d clips)", licenses_path, len(license_records))

    # Only written once every scene has a validated, licensed clip, so a
    # cached visuals.json can never describe a partially-fetched run.
    stage_path(run_id, CACHE_NAME).write_text(
        json.dumps(results, indent=2, ensure_ascii=False))
    log.info("visuals ok: %d/%d licensed clips fetched and validated", len(results), n)
    return results

"""Stage 9 - upload to YouTube via Data API v3 (OAuth installed-app flow).

Per channel (config.channel): its own client secret + token file, so each
channel's OAuth is fully separate - never a shared token. First run per
channel opens a browser for consent and caches config.channel.TOKEN_FILE;
later runs refresh silently. Long-form publishes immediately; Shorts are
uploaded private with a publishAt timestamp so YouTube publishes them on a
stagger.

Wrong-channel guard: before ANY upload (video, thumbnail, or Short), and again
right before setting a thumbnail, the authenticated account's real channel id
is checked against config.channel.YOUTUBE_CHANNEL_ID. A mismatch aborts loudly
rather than silently uploading to the wrong channel. This needs a read scope
(youtube.readonly) alongside youtube.upload - a token cached before this
change won't have it and will need one fresh re-auth.

A/S disclosure: status.containsSyntheticMedia (see settings.DISCLOSE_SYNTHETIC,
shared). AI_DISCLOSURE_LINE wording is per-channel (config.channel) since it
references the channel's own subject matter. Category 27 = Education.
selfDeclaredMadeForKids is False for both channels.
"""
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import config
from .util import get_logger, retry, load_json
from . import script_gen

log = get_logger("upload")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
         "https://www.googleapis.com/auth/youtube.readonly"]
CATEGORY_EDUCATION = "27"
MIN_CHAPTER_GAP = 10  # seconds; YouTube requires chapters to be spaced apart
SHORTS_HASHTAG = "#Shorts"
SHORT_EXTRA_HASHTAGS = 3      # on top of #Shorts itself


def _creds():
    token_file = config.channel.TOKEN_FILE
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001 - the refresh token itself can be dead
            # (revoked, or a "Testing"-status OAuth app's 7-day expiry) - fall
            # through to a fresh interactive login instead of crashing the run.
            log.warning("cached OAuth token refresh failed (%s); starting a "
                       "fresh interactive login", e)
            creds = None
    if not creds or not creds.valid:
        secret = config.channel.CLIENT_SECRET_FILE
        if not secret.exists():
            raise RuntimeError(
                f"OAuth client file not found: {secret}. "
                "Download the desktop client_secret.json from Google Cloud."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        log.info("saved new OAuth token to %s", token_file.name)
    return creds


def _service():
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def _verify_channel(youtube):
    """Abort before any upload if the authenticated account isn't the channel
    this config expects. Most important check in this module - an upload to
    the wrong channel is not easily undoable."""
    resp = youtube.channels().list(part="id", mine=True).execute()
    items = resp.get("items", [])
    actual = items[0]["id"] if items else None
    expected = config.channel.YOUTUBE_CHANNEL_ID
    if actual != expected:
        raise RuntimeError(
            f"WRONG CHANNEL: authenticated YouTube account is channel "
            f"'{actual}', but --channel {config.channel_slug} ({config.channel.NAME}) "
            f"expects '{expected}'. Aborting before any upload - check that "
            f"{config.channel.TOKEN_FILE} belongs to the right Google account."
        )
    log.info("channel verified: %s (%s)", config.channel.NAME, actual)


def _format_timestamp(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _chapters(run_id, script):
    """'0:00 Intro' + one line per body segment's resolved start time, at least
    MIN_CHAPTER_GAP seconds apart. Needs real word timings, so it reads the
    cached timings.json rather than estimating."""
    body_segments = [s for s in script.get("segments", []) if s.get("kind") == "body"]
    if not body_segments:
        return ["0:00 Intro"]
    try:
        timings = load_json(run_id, "timings.json")
    except FileNotFoundError:
        log.warning("no timings.json for %s; chapters will just be '0:00 Intro'", run_id)
        return ["0:00 Intro"]

    resolved = script_gen.resolve_segment_times(script["segments"], timings)
    resolved_body = [s for s in resolved if s.get("kind") == "body"]

    chapters = ["0:00 Intro"]
    last_time = 0.0
    for seg in resolved_body:
        if seg["start_time"] - last_time < MIN_CHAPTER_GAP:
            continue
        chapters.append(f"{_format_timestamp(seg['start_time'])} {seg['title']}")
        last_time = seg["start_time"]

    if len(chapters) < 3:
        log.warning("only %d chapters for %s (need >=3 for YouTube to show them)",
                   len(chapters), run_id)
    return chapters


def _description(run_id, script):
    chapters = _chapters(run_id, script)
    tags = " ".join(script.get("hashtags", []))
    parts = ["\n".join(chapters), script.get("description", ""), tags,
             config.channel.AI_DISCLOSURE_LINE]
    return "\n\n".join(p for p in parts if p.strip()).strip()


def _short_hashtags(script):
    """'#Shorts' plus up to SHORT_EXTRA_HASHTAGS of the script's own tags."""
    tags = [SHORTS_HASHTAG]
    for raw in script.get("hashtags", []):
        tag = (raw or "").strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        if tag.lower() == SHORTS_HASHTAG.lower():
            continue
        tags.append(tag)
        if len(tags) > SHORT_EXTRA_HASHTAGS:
            break
    return " ".join(tags)


def _short_description(script, short, long_video_id):
    """A Short's OWN description: one-line hook + a link back to the long-form
    published in the same run + #Shorts and a few tags.

    Deliberately NOT _description() - reusing the long-form copy (chapters,
    long blurb) on a Short buries the one thing that Short is for: sending the
    viewer to the full video. `short` is a shorts.json entry, whose hook/title
    came from the body segment that Short was cut from (see shorts._short_hook).
    """
    short = short or {}
    hook = (short.get("hook") or short.get("title") or script["title"]).strip()
    parts = [hook]
    if long_video_id:
        parts.append(f"Full video here: https://youtu.be/{long_video_id}")
    else:
        # Shouldn't happen - both call sites upload the long-form first - but a
        # missing link is better than shipping a broken "youtu.be/None".
        log.warning("no long-form video id; Short description will omit the link")
    parts.append(_short_hashtags(script))
    return "\n\n".join(p for p in parts if p.strip()).strip()


@retry(times=3, base_delay=10.0)
def _insert(youtube, path, snippet, status):
    body = {"snippet": snippet, "status": status}
    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body,
                                  media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def _set_thumbnail(youtube, video_id, thumb_path):
    _verify_channel(youtube)  # again, right before setting a thumbnail
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumb_path)).execute()
        log.info("thumbnail set")
    except Exception as e:  # noqa: BLE001
        log.warning("thumbnail set failed (non-fatal): %s", e)


def upload_long(run_id, script, video_path, thumb_path,
                privacy=None, dry_run=False):
    privacy = privacy or config.settings.LONGFORM_DEFAULT_PRIVACY
    if dry_run:
        log.info("[dry-run] target channel: %s (%s)", config.channel.NAME,
                 config.channel.YOUTUBE_CHANNEL_ID)
        log.info("[dry-run] would upload long-form '%s' as %s",
                 script["title"], privacy)
        log.info("[dry-run] description preview:\n%s", _description(run_id, script))
        return "dry-run-longform-id"

    youtube = _service()
    _verify_channel(youtube)
    snippet = {
        "title": script["title"][:100],
        "description": _description(run_id, script),
        "tags": [h.lstrip("#") for h in script.get("hashtags", [])][:15],
        "categoryId": CATEGORY_EDUCATION,
    }
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": config.settings.DISCLOSE_SYNTHETIC,
    }
    vid = _insert(youtube, video_path, snippet, status)
    log.info("long-form uploaded: https://youtu.be/%s", vid)
    if thumb_path:
        _set_thumbnail(youtube, vid, thumb_path)
    return vid


def upload_shorts(run_id, script, shorts, long_video_id=None,
                  offsets_hours=(6, 12), dry_run=False):
    """Upload each Short as private with a staggered publishAt time.

    long_video_id is the id returned by upload_long for THIS run - callers must
    upload the long-form first so each Short's description can link back to it.
    """
    results = []
    now = datetime.now(timezone.utc)

    if dry_run:
        log.info("[dry-run] target channel: %s (%s)", config.channel.NAME,
                 config.channel.YOUTUBE_CHANNEL_ID)
    else:
        youtube = _service()
        _verify_channel(youtube)

    for i, short in enumerate(shorts):
        publish_at = (now + timedelta(hours=offsets_hours[i]
                      if i < len(offsets_hours) else 12 * (i + 1)))
        title = f"{script['title']} #shorts"[:100]
        description = _short_description(script, short, long_video_id)
        if dry_run:
            log.info("[dry-run] would schedule short %d at %s",
                     i + 1, publish_at.isoformat())
            log.info("[dry-run] short %d description:\n%s", i + 1, description)
            results.append({"id": f"dry-short-{i+1}",
                            "publish_at": publish_at.isoformat()})
            continue
        snippet = {
            "title": title,
            "description": description,
            "tags": [h.lstrip("#") for h in script.get("hashtags", [])][:15],
            "categoryId": CATEGORY_EDUCATION,
        }
        status = {
            "privacyStatus": "private",
            "publishAt": publish_at.isoformat(),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": config.settings.DISCLOSE_SYNTHETIC,
        }
        vid = _insert(youtube, short["path"], snippet, status)
        log.info("short %d scheduled for %s: %s", i + 1,
                 publish_at.isoformat(), vid)
        results.append({"id": vid, "publish_at": publish_at.isoformat()})
    return results

"""Orchestrator - the full two-gate pipeline for one video.

Flow:
  1 topic
  2 script            -> GATE 1 (approve / edit-loop / reject-loop)
  3 voice + timings
  4 captions
  5 visuals
  6 assembly          -> build video.mp4
  7 shorts (cut, not yet published)
  8 thumbnail         -> GATE 2 (approve / edit-loop / reject-loop)
  9 publish long-form now; schedule Shorts staggered; mark topic done; heartbeat

--stage runs a single stage against cached upstream output (for testing).
--dry-run does everything except the real upload.
"""
from . import (topics, script_gen, voice, captions, visuals,
               assembly, shorts, thumbnail, review, upload)
from .config import config
from .util import get_logger, new_run_id

log = get_logger("pipeline")

STAGES = ["topic", "script", "voice", "captions", "visuals",
          "assembly", "shorts", "thumbnail", "upload"]


# --- single-stage runner (testing) -----------------------------------------

def _run_single(stage, run_id, short):
    """Run exactly one stage using cached upstream output. Used by --stage.

    Returns the run_id that was used so stages can be chained in testing.
    'topic' and 'script' can start a fresh run; every later stage needs --run-id.
    """
    if stage == "topic":
        topics.pick_topic()
        return run_id or "(none)"
    if stage == "script":
        rid = run_id or new_run_id()
        topic = topics.pick_topic()
        script_gen.generate_script(rid, topic, short=short, force=True)
        return rid

    if not run_id:
        raise RuntimeError("--stage %s needs --run-id from an earlier run." % stage)

    script = script_gen.load_cached(run_id)
    if stage == "voice":
        voice.generate_voice(run_id, script, force=True)
    elif stage == "captions":
        v = voice.generate_voice(run_id, script)
        captions.build_captions(run_id, v["timings"], force=True)
    elif stage == "visuals":
        visuals.fetch_visuals(run_id, script, force=True)
    elif stage == "assembly":
        v = voice.generate_voice(run_id, script)
        caps = captions.build_captions(run_id, v["timings"])
        vis = visuals.fetch_visuals(run_id, script)
        assembly.assemble(run_id, script, vis, caps, short=short, force=True)
    elif stage == "shorts":
        shorts.make_shorts(run_id, script, force=True)
    elif stage == "thumbnail":
        thumbnail.make_thumbnail(run_id, script, force=True)
    else:
        raise ValueError(f"unknown stage {stage}")
    return run_id


# --- gate loops -------------------------------------------------------------

def _script_gate(run_id, topic, short, dry_run=False):
    """Generate + Gate 1. Returns approved script, or None if rejected out."""
    script = script_gen.generate_script(run_id, topic, short=short)
    if dry_run:
        titles = script.get("titles") or []
        if titles:
            log.info("title candidates for %s:", run_id)
            for i, t in enumerate(titles, start=1):
                log.info("  [%d] (%s) %s", i, t["formula"], t["title"])
            try:
                raw = input(f"Pick a title [1-{len(titles)}, Enter={1}]: ").strip()
            except EOFError:
                log.info("no interactive input available; defaulting to title [1]")
                raw = ""
            idx = 0
            if raw:
                try:
                    idx = int(raw) - 1
                    if not (0 <= idx < len(titles)):
                        idx = 0
                except ValueError:
                    idx = 0
            script = script_gen.set_title(run_id, script, titles[idx]["title"])
        log.info("GATE 1 auto-approved (dry-run)")
        return script
    rejects = 0
    while True:
        decision = review.gate_script(run_id, script)
        action = decision["action"]
        if action == "approve":
            return script
        if action == "timeout":
            log.warning("gate 1 timed out; aborting run")
            return None
        if action == "reject":
            rejects += 1
            if rejects >= config.settings.MAX_REJECTS_PER_RUN:
                log.warning("gate 1 hit reject limit; aborting run")
                return None
            script = script_gen.generate_script(run_id, topic, short=short,
                                                force=True)
        elif action == "edit":
            script = script_gen.revise_script(run_id, decision["feedback"],
                                              short=short)


def _build_video(run_id, script, short):
    v = voice.generate_voice(run_id, script)
    caps = captions.build_captions(run_id, v["timings"])
    vis = visuals.fetch_visuals(run_id, script)
    assembly.assemble(run_id, script, vis, caps, short=short)
    sh = shorts.make_shorts(run_id, script)
    thumb = thumbnail.make_thumbnail(run_id, script)
    return sh, thumb


def _video_gate(run_id, script, short, dry_run=False):
    """Build + Gate 2. Returns (shorts, thumb) if approved, else None."""
    sh, thumb = _build_video(run_id, script, short)
    if dry_run:
        log.info("GATE 2 auto-approved (dry-run)")
        return sh, thumb
    rejects = 0
    while True:
        preview = sh[0]["path"] if sh else None
        decision = review.gate_video(run_id, script, thumb, preview_clip=preview)
        action = decision["action"]
        if action == "approve":
            return sh, thumb
        if action == "timeout":
            log.warning("gate 2 timed out; aborting run")
            return None
        if action == "reject":
            rejects += 1
            if rejects >= config.settings.MAX_REJECTS_PER_RUN:
                log.warning("gate 2 hit reject limit; aborting run")
                return None
            # Reject at video stage = new script, full rebuild.
            script = script_gen.generate_script(run_id,
                                                {"id": None, "title": script["title"]},
                                                short=short, force=True)
            sh, thumb = _build_video(run_id, script, short)
        elif action == "edit":
            fb = (decision["feedback"] or "").lower()
            if any(k in fb for k in ("script", "say", "word", "narrat", "title")):
                script = script_gen.revise_script(run_id, decision["feedback"],
                                                  short=short)
            # Re-fetch visuals + reassemble either way (most edits are visual).
            sh, thumb = _build_video(run_id, script, short)


# --- full run ---------------------------------------------------------------

def run(only=None, short=False, dry_run=False, forced_topic=None,
        run_id=None, privacy=config.settings.LONGFORM_DEFAULT_PRIVACY):
    if only:
        used = _run_single(only, run_id, short)
        log.info("stage '%s' done", only)
        return used

    run_id = run_id or new_run_id()
    log.info("=== run %s (short=%s dry_run=%s) ===", run_id, short, dry_run)
    if dry_run:
        log.info("=" * 60)
        log.info("DRY RUN - target channel: %s", config.channel.NAME)
        log.info("  channel id (expected): %s", config.channel.YOUTUBE_CHANNEL_ID)
        log.info("  token file: %s", config.channel.TOKEN_FILE)
        log.info("=" * 60)
    else:
        review.notify(f"AutoYouTube run {run_id} started.")

    try:
        topic = topics.pick_topic(forced_title=forced_topic)

        script = _script_gate(run_id, topic, short, dry_run=dry_run)
        if script is None:
            if not dry_run:
                review.notify(f"Run {run_id} stopped at script gate.")
            return run_id

        gated = _video_gate(run_id, script, short, dry_run=dry_run)
        if gated is None:
            if not dry_run:
                review.notify(f"Run {run_id} stopped at video gate.")
            return run_id
        sh, thumb = gated

        video_path = str((config.channel_output_dir / run_id / "video.mp4"))
        vid = upload.upload_long(run_id, script, video_path, thumb,
                                 privacy=privacy, dry_run=dry_run)
        # Long-form first (above), so each Short's description can link to it.
        upload.upload_shorts(run_id, script, sh, long_video_id=vid,
                             offsets_hours=config.settings.SHORTS_OFFSETS_HOURS,
                             dry_run=dry_run)

        from datetime import datetime, timezone
        topics.mark_published(topic["id"], vid,
                              datetime.now(timezone.utc).isoformat())
        if not dry_run:
            review.notify(f"Published: {script['title']} (https://youtu.be/{vid})")
        log.info("=== run %s complete ===", run_id)
    except Exception as e:  # noqa: BLE001
        log.exception("run failed")
        if not dry_run:
            review.notify(f"Run {run_id} FAILED: {e}")
        raise
    return run_id


# --- publish an already-built run (no Telegram) ------------------------------

def publish_existing(run_id, privacy=config.settings.LONGFORM_DEFAULT_PRIVACY):
    """Upload an already-built run's long-form + Shorts. No gates, no Telegram.

    Requires video.mp4 (and cached script) to already exist for run_id, i.e.
    the run made it through assembly already (normal run, or --stage chaining).
    """
    video_path = config.channel_output_dir / run_id / "video.mp4"
    if not video_path.exists():
        raise RuntimeError(
            f"No built video for run {run_id} ({video_path} missing). "
            "Build it first before publishing."
        )

    script = script_gen.load_cached(run_id)
    thumb = thumbnail.make_thumbnail(run_id, script)
    sh = shorts.make_shorts(run_id, script)

    vid = upload.upload_long(run_id, script, str(video_path), thumb,
                             privacy=privacy, dry_run=False)
    upload.upload_shorts(run_id, script, sh, long_video_id=vid,
                         offsets_hours=config.settings.SHORTS_OFFSETS_HOURS,
                         dry_run=False)
    log.info("published run %s: https://youtu.be/%s (privacy=%s)",
             run_id, vid, privacy)
    return vid

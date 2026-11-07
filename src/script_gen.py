"""Stage 2 - script generation via Gemini.

Produces one structured JSON object per video:
  - titles[]      5 title candidates, one per formula (for the reviewer to pick)
  - title         the chosen candidate (defaults to titles[0] until picked)
  - thumbnail_text  short punchy phrase for the thumbnail (NOT the title)
  - description, hashtags
  - segments[]    hook -> contract -> 5-7 named body segments (each a title +
                   its own narration slice)
  - narration     hook.text + contract.text + body texts, joined in order
  - scenes[]      scene-by-scene visual plan for later stages (unchanged shape)

Everything above comes from ONE Gemini call (body + hook + titles + the
visual-plan chunking), with the prompt instructing the model to write the body
first and only then the hook, so the hook can only promise what the body
delivers - "written after the body" is enforced by prompt ordering within a
single completion, not by a second API call (gemini-2.5-flash's free tier is
1,500 req/day; keeping this at 1 call/video, 2 worst-case, keeps real daily
usage nowhere near that).

The "contract" beat ("In this video: X, then Y, then Z") is built
deterministically from the first three body segment titles rather than
generated, so it is guaranteed specific and costs no extra call.

A word-count miss outside [SCRIPT_WORDS_HARD_MIN, SCRIPT_WORDS_HARD_MAX] costs
exactly one extra targeted call that asks the model to expand/trim the
EXISTING body draft (not regenerate hook/titles/anything else); if still out
of bounds after that, the closest draft is accepted rather than failing the
run.

narration/scenes keep their original flat shape so voice/captions/visuals/
assembly/shorts-rendering are unaffected; only shorts' window selection and
upload's chapter-building read the new `segments` field (via
resolve_segment_times below).
"""
import json
import re
import time

from google import genai
from google.genai import types

from .config import config
from .util import get_logger, save_json, load_json, cache_exists

log = get_logger("script")

CACHE_NAME = "script.json"

TITLE_FORMULAS = [
    "specific-number",
    "curiosity-gap",
    "transformation-promise",
    "warning-pattern",
    "why-reframe",
]


# --- prompts -----------------------------------------------------------------

def _identity_block():
    c = config.channel
    return (f'You are the writer for a faceless YouTube channel called "{c.NAME}" '
            f"about {c.CHANNEL_NICHE}. The audience is {c.CHANNEL_AUDIENCE}. "
            f"Narration style: {c.NARRATION_STYLE}.")


def _build_combined_prompt(topic, short=False):
    c = config.channel
    if short:
        n_segments = "1"
        words_per_segment = f"about {c.SCRIPT_WORDS_SHORT} words total"
        total_requirement = ""
    else:
        n_segments = "6 to 7"
        words_per_segment = ("180-220 words each (that's roughly 70-90s of narration "
                             "at natural spoken pace - err LONGER, not shorter)")
        # ~150 words reserved for hook+contract+outro overhead (not model-written
        # here); the body itself should land in this range.
        body_min = max(400, c.SCRIPT_WORDS_MIN - 150)
        body_max = max(body_min + 200, c.SCRIPT_WORDS_MAX - 150)
        total_requirement = (
            f"\nHard requirement: the combined word count of all body segments "
            f"together must be AT LEAST {body_min:,} words (aim for "
            f"{body_min:,}-{body_max:,} total). Undershooting this is a common "
            "mistake - if you're unsure whether a segment is long enough, add "
            "another 2-3 sentences of concrete detail, an example, or a number, "
            "rather than wrapping up early.")

    return f"""{_identity_block()}

Write a complete video script for this topic:
"{topic}"

Work through these steps IN ORDER, but return everything together in one JSON
object at the end.

STEP 1 - BODY (write this first, before the hook). Produce {n_segments} named
segments, each with a short title (2-5 words) and {words_per_segment}. Write it
as clean spoken prose - no headings, no stage directions, no "[music]"
markers, no greeting, no "in this video" framing. Segments must flow in a
logical order (context, then the main points, then a short conclusion with a
soft call to subscribe).
{total_requirement}

Exactly one segment, near the middle of the video, must contain one extra
re-engagement sentence (something like "stick with me, this next part is the
one that actually moves the needle") woven naturally into its narration -
mark that segment "reengage": true.

STEP 2 - HOOK (write this only after the body above, so it can only promise
what the body you just wrote actually delivers). Exactly three beats, clean
spoken prose (no greeting like "hey guys", no "in this video" framing - that's
handled separately):
  - claim: a bold claim or problem statement, 0-5s of speech. Every sentence
    under 10 words.
  - payoff: a specific promise of what the viewer gets, 5-15s of speech. Every
    sentence under 10 words.
  - curiosity: one curiosity loop that references something SPECIFIC from the
    body (a number, a name, a moment) that the body actually resolves, 15-30s
    of speech. Every sentence under 10 words.

STEP 3 - VISUAL PLAN. Split the hook (claim+payoff+curiosity, in order) into
sequential ~30-45 word chunks ("hook_scenes"), and separately split the body
segments' narration (in order) into sequential ~30-45 word chunks
("body_scenes"). Each set must cover its source text verbatim, in order, with
no gaps and no repeats. Give each chunk a short, concrete stock-footage search
query (2-4 words) a site like Pixabay would return good clips for - prefer
literal, filmable subjects ("person counting coins", "city skyline sunset")
over abstract ideas.

STEP 4 - TITLES + THUMBNAIL. Produce:
- 5 title candidates, one for each of these formulas, each with the primary
  keyword in the first 4-5 words, 40-65 characters, using "you"/"I" where
  natural, and never promising something the body doesn't deliver:
  1. specific-number   (e.g. "7 Index Funds That Beat the S&P")
  2. curiosity-gap      (e.g. "Why Your 401k Is Quietly Losing Money")
  3. transformation-promise (e.g. "How I Turned $500 Into a Real Portfolio")
  4. warning-pattern    (e.g. "Stop Buying Index Funds Until You Watch This")
  5. why-reframe        (e.g. "Why Index Funds Beat Stock Picking")
- a thumbnail_text: a SEPARATE short phrase, 3-4 words, punchy, all-caps-worthy,
  that is NOT the same as any of the 5 titles (it sits ON the thumbnail image,
  the title sits below the video). E.g. if a title is "7 Index Funds That Beat
  the S&P", thumbnail_text could be "7 FUNDS THAT WIN".
- a 2-3 sentence YouTube description.
- 3-6 hashtags.

Return ONLY a JSON object with exactly this shape, no markdown fences:
{{
  "segments": [
    {{"title": "short segment title", "text": "narration for this segment", "reengage": false}}
  ],
  "hook": {{"claim": "...", "payoff": "...", "curiosity": "..."}},
  "hook_scenes": [
    {{"text": "chunk of the hook this scene covers", "visual_query": "...", "type": "stock"}}
  ],
  "body_scenes": [
    {{"text": "chunk of the body this scene covers", "visual_query": "...", "type": "stock"}}
  ],
  "titles": [
    {{"formula": "specific-number", "title": "..."}},
    {{"formula": "curiosity-gap", "title": "..."}},
    {{"formula": "transformation-promise", "title": "..."}},
    {{"formula": "warning-pattern", "title": "..."}},
    {{"formula": "why-reframe", "title": "..."}}
  ],
  "thumbnail_text": "3-4 word phrase",
  "description": "2-3 sentence YouTube description",
  "hashtags": ["#finance", "..."]
}}"""


def _build_fix_length_prompt(body_segments, direction, target_min, target_max):
    action = "Expand" if direction == "expand" else "Trim"
    detail = ("adding concrete detail, examples, or numbers to existing segments"
             if direction == "expand" else
             "tightening prose and cutting redundancy")
    segs_json = json.dumps(
        [{"title": s["title"], "text": s["text"], "reengage": bool(s.get("reengage"))}
         for s in body_segments], ensure_ascii=False)

    return f"""Here are the body segments of a video script narration, as JSON:
{segs_json}

{action} this body narration by {detail} so the TOTAL word count across all
segments lands between {target_min} and {target_max} words. Keep the same
number of segments, the same titles, the same segment marked "reengage", and
the same meaning and order - only adjust length.

Also provide an updated scene-by-scene visual plan covering the entire revised
body narration verbatim, in order, in ~30-45 word chunks, each with a short
concrete stock-footage search query (2-4 words).

Return ONLY a JSON object with exactly this shape, no markdown fences:
{{
  "segments": [{{"title": "...", "text": "...", "reengage": false}}],
  "scenes": [{{"text": "...", "visual_query": "...", "type": "stock"}}]
}}"""


# --- gemini plumbing ----------------------------------------------------------

def _clean_json(raw):
    """Gemini usually honours response_mime_type=json, but strip fences just in case."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _should_use_fallback_model(exc):
    """True for a 429 RESOURCE_EXHAUSTED or 503 UNAVAILABLE specifically, not
    other errors - retries against the same model won't fix a quota
    exhaustion or an overloaded model, but switching to the fallback model
    (same key, separate quota pool) will."""
    code = getattr(exc, "code", None)
    if code in (429, 503):
        return True
    msg = str(exc)
    return "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg


def _call_gemini_with_model(prompt, model):
    client = genai.Client(api_key=config.gemini_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.9,
        ),
    )
    return resp.text


def _call_gemini(prompt):
    """Calls the primary model with exponential-backoff retry for transient
    errors. A 429 RESOURCE_EXHAUSTED or 503 UNAVAILABLE short-circuits that
    retry loop immediately (backoff won't clear a quota exhaustion or an
    overloaded model) and makes exactly one attempt against
    GEMINI_MODEL_FALLBACK instead - same API key, no key rotation, just a
    different model with its own separate quota pool on the same project."""
    delay = 5.0
    tries = 3
    for attempt in range(1, tries + 1):
        try:
            return _call_gemini_with_model(prompt, config.settings.GEMINI_MODEL)
        except Exception as e:  # noqa: BLE001
            if _should_use_fallback_model(e):
                log.warning("primary model %s hit %s; retrying once on "
                           "fallback model %s (same key)",
                           config.settings.GEMINI_MODEL, e,
                           config.settings.GEMINI_MODEL_FALLBACK)
                return _call_gemini_with_model(prompt, config.settings.GEMINI_MODEL_FALLBACK)
            if attempt == tries:
                log.error("_call_gemini failed after %d attempts: %s", tries, e)
                raise
            log.warning("_call_gemini attempt %d/%d failed (%s); retrying in %.0fs",
                       attempt, tries, e, delay)
            time.sleep(delay)
            delay *= 2


def _ask(prompt):
    return json.loads(_clean_json(_call_gemini(prompt)))


def _ask_strict(prompt):
    """Like _ask, but for the combined call: response_mime_type=json means
    Gemini should return pure JSON with no fences and no trailing prose, so
    this parses directly with json.loads (no fence-stripping tolerance). If
    parsing still fails, the raw response is logged (truncated) before
    raising, so the actual bad output is visible rather than just the
    parser's complaint."""
    raw = _call_gemini(prompt)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("combined-call JSON parse failed (%s); raw response "
                 "(first 500 chars): %r", e, raw[:500])
        raise


# --- validation ---------------------------------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text):
    return [s for s in _SENTENCE_RE.split((text or "").strip()) if s]


def _max_sentence_words(text):
    lens = [len(s.split()) for s in split_sentences(text)]
    return max(lens, default=0)


def _validate_scenes_list(data, key):
    for sc in data.get(key) or []:
        if not sc.get("text") or not sc.get("visual_query"):
            raise ValueError(f"{key} entry missing text/visual_query")


def _validate_combined(data, short=False):
    required = {"segments", "hook", "hook_scenes", "body_scenes",
               "titles", "thumbnail_text", "description", "hashtags"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"combined response missing keys: {missing}")

    if len(data["titles"]) != 5:
        raise ValueError(f"expected 5 titles, got {len(data['titles'])}")
    titles_lower = {t["title"].strip().lower() for t in data["titles"]}
    if data["thumbnail_text"].strip().lower() in titles_lower:
        raise ValueError("thumbnail_text must differ from every title")

    n_min, n_max = (1, 1) if short else (5, 7)
    if not (n_min <= len(data["segments"]) <= n_max):
        raise ValueError(f"expected {n_min}-{n_max} body segments, got {len(data['segments'])}")
    for seg in data["segments"]:
        if not seg.get("title") or not seg.get("text", "").strip():
            raise ValueError("body segment missing title/text")
    if not short and not any(seg.get("reengage") for seg in data["segments"]):
        raise ValueError("no body segment marked reengage")

    hook = data.get("hook") or {}
    hook_required = {"claim", "payoff", "curiosity"}
    if hook_required - hook.keys():
        raise ValueError(f"hook missing keys: {hook_required - hook.keys()}")
    for key in hook_required:
        if not hook[key].strip():
            raise ValueError(f"hook.{key} is empty")
        words = _max_sentence_words(hook[key])
        if words >= 10:
            raise ValueError(f"hook.{key} has a sentence with {words} words (must be <10)")

    _validate_scenes_list(data, "hook_scenes")
    _validate_scenes_list(data, "body_scenes")
    return data


def _try(fn, tries, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying on ValueError (content-validation
    failures) up to `tries` times. Network/quota errors are already retried
    (and fallback-keyed) inside _call_gemini."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            last = e
            log.warning("%s attempt %d/%d rejected: %s", fn.__name__, attempt, tries, e)
    raise RuntimeError(f"{fn.__name__} failed validation after {tries} attempts: {last}")


# --- generation ----------------------------------------------------------------

def _contract_beat(body_segments):
    picks = [s["title"] for s in body_segments[:3]]
    while len(picks) < 3:
        picks.append(picks[-1] if picks else "the rest")
    return f"In this video: {picks[0]}, then {picks[1]}, then {picks[2]}."


def _generate_combined(topic, short):
    def go():
        return _validate_combined(_ask_strict(_build_combined_prompt(topic, short=short)),
                                  short=short)
    return _try(go, tries=3)


def _word_count(text):
    return len(text.split())


def _combined_body_words(combined):
    return sum(_word_count(seg["text"]) for seg in combined["segments"])


def _combined_word_count(combined):
    hook = combined["hook"]
    hook_words = sum(_word_count(hook[k]) for k in ("claim", "payoff", "curiosity"))
    contract_words = _word_count(_contract_beat(combined["segments"]))
    return hook_words + contract_words + _combined_body_words(combined)


def _fix_length(combined):
    """One targeted call asking the model to expand/trim the EXISTING body
    draft to hit the hard word-count bounds - does not touch hook/titles, and
    does not regenerate from scratch. Falls back to the original draft on any
    failure (bad JSON, still-missing fields, network exhaustion)."""
    c = config.channel
    body_segments = combined["segments"]
    fixed_words = _combined_word_count(combined) - _combined_body_words(combined)
    target_min = max(200, c.SCRIPT_WORDS_HARD_MIN - fixed_words)
    target_max = max(target_min + 100, c.SCRIPT_WORDS_HARD_MAX - fixed_words)
    current_words = _combined_body_words(combined)
    direction = "expand" if current_words < target_min else "trim"

    try:
        fixed = _ask(_build_fix_length_prompt(body_segments, direction, target_min, target_max))
        new_body = fixed.get("segments")
        if not new_body:
            raise ValueError("length-fix response missing segments")
        for seg in new_body:
            if not seg.get("title") or not seg.get("text", "").strip():
                raise ValueError("length-fix segment missing title/text")
    except Exception as e:  # noqa: BLE001 - one best-effort pass; keep original on any failure
        log.warning("length-fix call failed (%s); keeping original draft", e)
        return combined

    combined = {**combined, "segments": new_body}
    if fixed.get("scenes"):
        combined["body_scenes"] = fixed["scenes"]
    log.info("length-fix: body now %d words (target %d-%d, was %d)",
             _combined_body_words(combined), target_min, target_max, current_words)
    return combined


def _assemble(combined):
    hook = combined["hook"]
    hook_text = " ".join(t for t in (hook["claim"], hook["payoff"], hook["curiosity"])
                         if t.strip())
    contract_text = _contract_beat(combined["segments"])

    body_segments = [{"kind": "body", "title": seg["title"], "text": seg["text"],
                      "reengage": bool(seg.get("reengage"))}
                     for seg in combined["segments"]]

    outro_text = config.channel.OUTRO_TEXT

    hook_scenes = combined.get("hook_scenes") or []
    body_scenes = combined.get("body_scenes") or []
    contract_query = (body_scenes[0]["visual_query"] if body_scenes
                      else "confident person talking to camera")
    outro_query = (body_scenes[-1]["visual_query"] if body_scenes
                  else "confident person talking to camera")
    scenes = [*hook_scenes,
             {"text": contract_text, "visual_query": contract_query, "type": "stock"},
             *body_scenes,
             {"text": outro_text, "visual_query": outro_query, "type": "stock"}]

    segments = [
        {"kind": "hook", "title": "Hook", "text": hook_text},
        {"kind": "contract", "title": "In This Video", "text": contract_text},
        *body_segments,
        {"kind": "outro", "title": "Outro", "text": outro_text},
    ]
    narration = " ".join(s["text"] for s in segments)

    return {
        "titles": combined["titles"],
        "title": combined["titles"][0]["title"],
        "thumbnail_text": combined["thumbnail_text"],
        "description": combined["description"],
        "hashtags": combined["hashtags"],
        "segments": segments,
        "narration": narration,
        "scenes": scenes,
    }


def generate_script(run_id, topic, short=False, force=False):
    """Generate (or load cached) script JSON for a topic.

    force: ignore cache and regenerate (used on 'reject').

    Normal case: 1 Gemini call. Worst case (narration lands outside the hard
    word-count bounds): 2 calls total - the second is a single targeted
    expand/trim pass over the existing body draft, not a regenerate.

    Channels that set config.channel.SCRIPT_STYLE = "history_mystery" (only
    the history channel today) are routed to script_gen_history.py's
    draft+self-critique generator instead - see that module. Any channel that
    doesn't set SCRIPT_STYLE (clearcents, mindunpacked) takes the path below,
    unchanged."""
    if cache_exists(run_id, CACHE_NAME) and not force:
        log.info("using cached script")
        return load_json(run_id, CACHE_NAME)

    log.info("generating script for: %s", topic["title"])

    if getattr(config.channel, "SCRIPT_STYLE", None) == "history_mystery":
        from . import script_gen_history
        data = script_gen_history.generate_history_script(topic["title"], short=short)
    else:
        c = config.channel
        combined = _generate_combined(topic["title"], short)

        if not short:
            words = _combined_word_count(combined)
            if not (c.SCRIPT_WORDS_HARD_MIN <= words <= c.SCRIPT_WORDS_HARD_MAX):
                log.warning("narration word count %d outside hard bounds [%d, %d]; "
                           "sending one targeted length-fix call (not a regenerate)",
                           words, c.SCRIPT_WORDS_HARD_MIN, c.SCRIPT_WORDS_HARD_MAX)
                combined = _fix_length(combined)
                words = _combined_word_count(combined)
                if not (c.SCRIPT_WORDS_HARD_MIN <= words <= c.SCRIPT_WORDS_HARD_MAX):
                    log.warning("still %d words after the length-fix pass; "
                               "accepting closest draft rather than failing", words)

        data = _assemble(combined)

    words = _word_count(data["narration"])
    log.info("script ok: '%s' (%d words, %d segments, %d scenes)",
             data["title"], words, len(data["segments"]), len(data["scenes"]))

    save_json(run_id, CACHE_NAME, data)
    return data


def set_title(run_id, script, title):
    """Persist the reviewer's title pick (dry-run gate-1 or Telegram gate)."""
    script["title"] = title
    save_json(run_id, CACHE_NAME, script)
    log.info("title set: %s", title)
    return script


def load_cached(run_id):
    """Load an already-generated script for this run (used by later stages)."""
    if not cache_exists(run_id, CACHE_NAME):
        raise RuntimeError(
            f"No cached script for run {run_id}. Run the script stage first."
        )
    return load_json(run_id, CACHE_NAME)


def revise_script(run_id, feedback, short=False):
    """Apply free-text edit feedback to the cached script (used on 'edit')."""
    current = load_json(run_id, CACHE_NAME)
    prompt = f"""Here is an existing YouTube video script as JSON:

{json.dumps(current, ensure_ascii=False)}

Apply this change requested by the reviewer: "{feedback}"

Keep the same JSON shape and keys. Only change what the feedback asks for.
If you change any segment's "text", also update the top-level "narration" field
to be the concatenation of all segments' "text" in order (same as before, just
reflecting the edit).
Return ONLY the updated JSON object, no markdown fences."""
    data = _ask(prompt)
    save_json(run_id, CACHE_NAME, data)
    log.info("script revised per feedback: %s", feedback)
    return data


def resolve_segment_times(segments, timings):
    """Map each segment to real start_time/end_time (seconds) using the actual
    edge-tts word timings for the full narration (only real audio timing is
    accurate - this can't be estimated at script-gen time).

    Assumes `segments`' texts were joined (in order, single-space-separated)
    to form the narration that was sent to edge-tts, so cumulative word counts
    line up with the flat `timings` list."""
    resolved = []
    cursor = 0
    n_timings = len(timings)
    for seg in segments:
        n = max(1, len(seg["text"].split()))
        start_idx = min(cursor, n_timings - 1) if n_timings else 0
        end_idx = min(cursor + n - 1, n_timings - 1) if n_timings else 0
        start_time = timings[start_idx]["start"] if n_timings else 0.0
        end_time = timings[end_idx]["end"] if n_timings else 0.0
        resolved.append({**seg, "start_time": round(start_time, 2),
                         "end_time": round(end_time, 2), "word_start": cursor})
        cursor += n
    return resolved


def sentence_boundary_time(seg, timings, max_time, min_time=0.0):
    """Real end-time (seconds) of the last sentence inside `seg` (a resolved
    segment, i.e. one with "word_start" from resolve_segment_times) that ends
    at or before max_time and at or after min_time, or None if none qualifies.

    edge-tts's WordBoundary events strip sentence punctuation from the word
    text itself, so sentence boundaries can't be detected from timings alone -
    they're found in the segment's original text (which still has real
    punctuation) and then mapped back to a real timestamp by word count."""
    n_timings = len(timings)
    words_seen = 0
    best = None
    for sentence in split_sentences(seg["text"]):
        words_seen += max(1, len(sentence.split()))
        idx = seg["word_start"] + words_seen - 1
        if idx >= n_timings:
            break
        t = timings[idx]["end"]
        if t > max_time:
            break
        if t >= min_time:
            best = t
    return best

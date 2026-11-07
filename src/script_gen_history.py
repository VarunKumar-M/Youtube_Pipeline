"""History channel's script generator - selected via
config.channel.SCRIPT_STYLE = "history_mystery" (see script_gen.generate_script,
which dispatches here; every other channel keeps script_gen.py's generic path
unchanged).

Reuses script_gen.py's Gemini plumbing (_call_gemini, _ask_strict, _try,
word-count helpers, _fix_length) rather than duplicating it - this module only
supplies a different PROMPT and a stricter, history-specific VALIDATOR, plus a
second "self-critique" model call.

THE PROBLEM THIS FIXES: generic encyclopedia-summary narration ("X happened in
year Y. It was important because Z.") reads as inauthentic/reused content to
YouTube's review systems and bores viewers. This channel's lens is
MYSTERY / UNTOLD - every topic, whatever it is, is framed as something hidden,
surprising, forgotten, or not what people were taught - and every script must
read as a STORY (cold open, rising concrete detail, a real turn, resolution),
never a summary.

Two-pass generation: a draft call (prompted + structurally validated), then a
SECOND call that critiques the draft against the same checklist a human editor
would use (hooks in 15s? real turn? enough concrete specifics? story or
summary?) and rewrites weak sections. This second call is what turns a
plausible-looking draft into something worth watching - see the module's
_critique(). If the critique call fails or returns something that doesn't
validate, the pre-critique (already-valid) draft is kept rather than failing
the run - same safety-net pattern as script_gen._fix_length.

Two of the body segments are marked "short_ready": true - written by the
model as complete, self-contained mini-stories in their own right (their own
hook, one surprising beat, a button line) about two DIFFERENT beats. shorts.py
prefers these when choosing which two segments to cut into Shorts (see
shorts._pick_segments) instead of "first segment + longest remaining" - for
every other channel this field is never set, so that preference is a no-op
and shorts.py's behaviour there is unchanged.
"""
import json
import re

from .config import config
from .util import get_logger
from . import script_gen

log = get_logger("script_history")

LENS = ("Every video is framed through a MYSTERY / UNTOLD lens: something "
       "hidden, surprising, forgotten, or not what people were taught - no "
       "matter what the topic is. The lens is what unifies the channel, not "
       "the subject matter.")

DEFAULT_BANNED_OPENERS = [
    "in this video", "today we", "today we'll", "have you ever wondered",
    "welcome to", "did you know that", "let's talk about", "let's dive into",
]
DEFAULT_BANNED_FILLERS = [
    "many people", "it was a difficult time", "changed everything",
    "changed the world forever", "a lot of people", "things were different",
]
DEFAULT_BANNED_SCREEN_REFS = [
    "here we see", "as you can see", "in this clip", "in this footage", "on screen",
]
DEFAULT_MIN_CONCRETE_SPECIFICS = 6

_NUMBER_RE = re.compile(r"\b\d[\d,]*\b")


# --- prompts -----------------------------------------------------------------

def _identity_block():
    c = config.channel
    return (f'You are the writer for a faceless YouTube history channel called '
            f'"{c.NAME}". The audience is {c.CHANNEL_AUDIENCE}. Narration style: '
            f'{c.NARRATION_STYLE}.')


def _length_block(short):
    c = config.channel
    if short:
        return "1", f"about {c.SCRIPT_WORDS_SHORT} words total", ""
    n_segments = "6 to 7"
    words_per_segment = ("180-220 words each (roughly 70-90s of narration at "
                         "natural spoken pace - err LONGER, not shorter)")
    body_min = max(400, c.SCRIPT_WORDS_MIN - 150)
    body_max = max(body_min + 200, c.SCRIPT_WORDS_MAX - 150)
    total_requirement = (
        f"\nHard requirement: the combined word count of all body segments "
        f"together must be AT LEAST {body_min:,} words (aim for "
        f"{body_min:,}-{body_max:,} total). Undershooting this is a common "
        "mistake - if unsure whether a segment is long enough, add another "
        "2-3 sentences of concrete detail rather than wrapping up early.")
    return n_segments, words_per_segment, total_requirement


def _build_history_prompt(topic_title, short=False):
    c = config.channel
    n_segments, words_per_segment, total_requirement = _length_block(short)
    banned_openers = getattr(c, "SCRIPT_BANNED_OPENERS", DEFAULT_BANNED_OPENERS)
    banned_fillers = getattr(c, "SCRIPT_BANNED_FILLERS", DEFAULT_BANNED_FILLERS)
    banned_screen_refs = getattr(c, "SCRIPT_BANNED_SCREEN_REFS", DEFAULT_BANNED_SCREEN_REFS)
    min_specifics = getattr(c, "SCRIPT_MIN_CONCRETE_SPECIFICS", DEFAULT_MIN_CONCRETE_SPECIFICS)

    shape_and_turn = "" if short else """
STEP 0 - SHAPE. Pick whichever narrative shape best fits this topic - do not
force one template onto every topic:
  - EVENT: build to a single moment (a day, a disaster, a decision), tension
    rising to it, then the aftermath.
  - PERSON: one person's arc - what they wanted, what went wrong, the turn,
    what it cost.
  - MYSTERY: a question posed cold, evidence laid out, the reveal or the "we
    still don't know."
Record your choice in "narrative_shape" (exactly "EVENT", "PERSON", or
"MYSTERY"). Also decide the TURN - the surprise, the reveal, the moment the
story flips against what the viewer assumed at the start. Summarize it in one
sentence in "turn". The body must actually deliver this turn, not just assert
that something surprising happened.
"""

    short_ready_instruction = "" if short else """
Exactly 2 DIFFERENT body segments must each also work as a complete,
self-contained ~60-second story with ZERO context from the rest of the video -
someone should be able to watch just that one segment alone and get a full
mini-story: its own hook-like opening line, one surprising concrete beat, and
a closing line that reads like a button (a small, satisfying final beat) -
while still fitting naturally into the overall body flow where it sits. Mark
each with "short_ready": true. Pick two DIFFERENT beats/moments for these two
segments - never the same beat twice. Optionally mark whichever segment
contains the story's TURN with "is_turn": true.
"""

    schema_shape_fields = ("" if short else
                           '  "narrative_shape": "EVENT",\n  "turn": "one sentence describing the reveal",\n')
    schema_segment_extra = "" if short else ', "short_ready": false, "is_turn": false'

    return f"""{_identity_block()}

{LENS}

Write a complete video script for this topic:
"{topic_title}"

THE PROBLEM YOU MUST AVOID: encyclopedia-summary narration ("X happened in
year Y. It was important because Z."). That reads as inauthentic/reused
content to YouTube's review systems and bores viewers. Write a STORY with
narrative tension, not a summary of a topic.
{shape_and_turn}
Work through these steps in order, but return everything together in one JSON
object at the end.

STEP 1 - BODY (write this first, before the hook). Produce {n_segments} named
segments, each with a short title (2-5 words) and {words_per_segment}. Write it
as clean spoken prose - no headings, no stage directions, no "[music]"
markers. The narration must stand alone as AUDIO ONLY with no footage at all -
never describe what's on screen. Banned screen-description phrases:
{", ".join(repr(p) for p in banned_screen_refs)}.
{total_requirement}

RISING DETAIL: every claim should be concrete and checkable - real names, real
dates, real numbers, real quotes. The body must contain at least
{min_specifics} distinct concrete specifics (numbers, dates, quantities).
Banned vague filler with no number attached: {", ".join(repr(p) for p in banned_fillers)}.
No Wikipedia-cadence summary sentences.

Include a STAKES line early in the body: why this matters, why it's strange,
or what everyone gets wrong about it.

End the body with a RESOLUTION and a short reflective closing line (not a
recap - a genuine "what this means" beat).

Exactly one segment, near the middle, must contain one extra re-engagement
sentence (something like "stick with me, this next part is the one that
actually changes the story") woven naturally into its narration - mark that
segment "reengage": true.
{short_ready_instruction}
STEP 2 - HOOK (write this only after the body, so it can only promise what the
body actually delivers). Exactly three beats, clean spoken prose (no greeting
like "hey guys"):
  - claim: a COLD OPEN, 0-5s of speech. Drop the viewer straight into a
    specific scene, moment, or a sharp question. NEVER a definition, a bare
    date, or any of these banned openers: {", ".join(repr(p) for p in banned_openers)}.
    Every sentence under 10 words.
  - payoff: a specific promise of what the viewer gets, 5-15s of speech. Every
    sentence under 10 words.
  - curiosity: one curiosity loop referencing something SPECIFIC from the body
    (a number, a name, a moment) that the body actually resolves, 15-30s of
    speech. Every sentence under 10 words.

STEP 3 - VISUAL PLAN. Split the hook (claim+payoff+curiosity, in order) into
sequential ~30-45 word chunks ("hook_scenes"), and separately split the body
segments' narration (in order) into sequential ~30-45 word chunks
("body_scenes"). Each set must cover its source text verbatim, in order, with
no gaps and no repeats. Give each chunk a short, concrete ARCHIVAL FOOTAGE
search query (2-4 words) suited to a newsreel/archival library search (era,
subject, place, event - e.g. "1930s soup kitchen", "apollo launch pad") rather
than generic stock footage. Do NOT lead a query with a specific named
individual's name (e.g. NOT "margaret hamilton mit nasa") - footage of a
named person is almost never separately licensed, so that query nearly always
turns up nothing. Describe the SCENE, ROLE, or SETTING instead (e.g. "nasa
software engineers 1960s", "mission control room apollo era").

STEP 4 - TITLES + THUMBNAIL. Produce:
- 5 title candidates, one for each of these formulas, each with the primary
  keyword in the first 4-5 words, 40-65 characters, and never promising
  something the body doesn't deliver:
  1. specific-number   (e.g. "5 Photos That Rewrote a War")
  2. curiosity-gap      (e.g. "The Warning Everyone on Board Ignored")
  3. transformation-promise (e.g. "How One Photograph Changed the Verdict")
  4. warning-pattern    (e.g. "Don't Trust This Photo Until You Watch This")
  5. why-reframe        (e.g. "Why This 'Accident' Wasn't One")
- a thumbnail_text: a SEPARATE short phrase, 3-4 words, punchy, all-caps-worthy,
  different from every title.
- a 2-3 sentence YouTube description.
- 3-6 hashtags.

STEP 5 - OUTRO LINE. Write one short subscribe line for the end of the video,
narrated aloud (also shown on screen). It must contain a clear call to
subscribe, fit a "forgotten stories" framing, and use DIFFERENT wording than a
generic template - vary it per video, don't reuse the exact same sentence
every time. Put it in "outro_line".

Return ONLY a JSON object with exactly this shape, no markdown fences:
{{
{schema_shape_fields}  "segments": [
    {{"title": "short segment title", "text": "narration for this segment", "reengage": false{schema_segment_extra}}}
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
  "hashtags": ["#history", "..."],
  "outro_line": "one subscribe line, on-screen + narrated"
}}"""


def _build_critique_prompt(topic_title, draft, short=False):
    min_specifics = getattr(config.channel, "SCRIPT_MIN_CONCRETE_SPECIFICS",
                            DEFAULT_MIN_CONCRETE_SPECIFICS)
    short_ready_check = "" if short else """
6. Are the two "short_ready" segments genuinely self-contained mini-stories
   (their own hook, one surprising beat, a button line) about two DIFFERENT
   moments - not the same beat twice?"""
    checklist = f"""Check the draft against every one of these, and REWRITE any
part that fails:
1. Does the cold open (hook.claim) hook a viewer in the first 15 seconds -
   dropping them into a scene or a sharp question - with NO definition, NO
   bare date, and none of the banned openers?
2. Is there a real TURN - a genuine reveal that flips the story - not just an
   assertion that something surprising happened?
3. Does the body contain at least {min_specifics} distinct concrete specifics
   (real names, dates, numbers)? Count them. If short, add more.
4. Does it read as a STORY with rising tension, or does any part read like an
   encyclopedia/Wikipedia summary ("X happened in year Y...")? Rewrite any
   summary-cadence sentence into scene, tension, or consequence.
5. Does any line describe what's on screen rather than standing alone as
   audio? Remove it.{short_ready_check}"""

    return f"""{_identity_block()}

Here is a draft video script for the topic "{topic_title}", as JSON:
{json.dumps(draft, ensure_ascii=False)}

{checklist}

Rewrite whatever is weak. Keep the same JSON shape and keys, the same overall
segment count and order, and the same approximate body length. Return ONLY
the full IMPROVED JSON object, no markdown fences, no commentary - the
complete script, not just the changed parts."""


# --- validation ---------------------------------------------------------------

def _first_banned(text, phrases):
    low = (text or "").lower()
    for p in phrases:
        if p in low:
            return p
    return None


def _count_concrete_specifics(segments):
    text = " ".join(s.get("text", "") for s in segments)
    return len(_NUMBER_RE.findall(text))


def _validate_history(data, short=False):
    script_gen._validate_combined(data, short=short)

    c = config.channel
    banned_openers = getattr(c, "SCRIPT_BANNED_OPENERS", DEFAULT_BANNED_OPENERS)
    banned_fillers = getattr(c, "SCRIPT_BANNED_FILLERS", DEFAULT_BANNED_FILLERS)
    banned_screen_refs = getattr(c, "SCRIPT_BANNED_SCREEN_REFS", DEFAULT_BANNED_SCREEN_REFS)
    min_specifics = getattr(c, "SCRIPT_MIN_CONCRETE_SPECIFICS", DEFAULT_MIN_CONCRETE_SPECIFICS)

    hit = _first_banned(data["hook"]["claim"], banned_openers)
    if hit:
        raise ValueError(f"cold open (hook.claim) contains banned opener: {hit!r}")

    all_text = [data["hook"]["claim"], data["hook"]["payoff"], data["hook"]["curiosity"],
               *[s["text"] for s in data["segments"]]]
    for text in all_text:
        hit = _first_banned(text, banned_fillers)
        if hit:
            raise ValueError(f"vague filler banned phrase found: {hit!r}")
        hit = _first_banned(text, banned_screen_refs)
        if hit:
            raise ValueError(f"screen-description phrase found (narration "
                             f"must stand alone as audio): {hit!r}")

    if not short:
        shape = data.get("narrative_shape")
        if shape not in ("EVENT", "PERSON", "MYSTERY"):
            raise ValueError(f"narrative_shape must be EVENT/PERSON/MYSTERY, got {shape!r}")

        if not (data.get("turn") or "").strip():
            raise ValueError("missing non-empty 'turn' (the reveal/twist sentence)")

        n_short_ready = sum(1 for s in data["segments"] if s.get("short_ready"))
        if n_short_ready < 2:
            raise ValueError(f"need >=2 body segments marked short_ready (self-contained "
                             f"Short mini-stories), got {n_short_ready}")

        specifics = _count_concrete_specifics(data["segments"])
        if specifics < min_specifics:
            raise ValueError(f"only {specifics} concrete specifics (numbers/dates) in "
                             f"the body, need >= {min_specifics}")

    outro_line = (data.get("outro_line") or "").strip()
    if not outro_line:
        raise ValueError("missing non-empty 'outro_line'")
    if "subscribe" not in outro_line.lower():
        raise ValueError(f"outro_line must contain a subscribe call-to-action: {outro_line!r}")

    return data


# --- generation ----------------------------------------------------------------

def _generate_draft(topic_title, short):
    def go():
        data = script_gen._ask_strict(_build_history_prompt(topic_title, short=short))
        return _validate_history(data, short=short)
    return script_gen._try(go, tries=3)


def _critique(topic_title, draft, short):
    """One extra model call: critique the draft against the same checklist a
    human editor would use, and rewrite weak sections. Falls back to the
    already-valid draft on any failure - this pass is a quality upgrade, not
    a dependency the run can't survive without."""
    try:
        raw = script_gen._call_gemini(_build_critique_prompt(topic_title, draft, short=short))
        improved = json.loads(raw)
        return _validate_history(improved, short=short)
    except Exception as e:  # noqa: BLE001
        log.warning("self-critique pass failed or produced an invalid rewrite "
                   "(%s); keeping the pre-critique draft", e)
        return draft


def _fix_length_preserving_markers(combined):
    """script_gen._fix_length() only round-trips title/text/reengage through
    its own model call, so short_ready/is_turn would silently vanish from any
    segment that survives it. It guarantees the same segment count and order,
    so they're reattached here by position rather than re-asking the model."""
    original_segments = combined["segments"]
    fixed = script_gen._fix_length(combined)
    for i, seg in enumerate(fixed["segments"]):
        if i < len(original_segments):
            seg.setdefault("short_ready", original_segments[i].get("short_ready", False))
            seg.setdefault("is_turn", original_segments[i].get("is_turn", False))
    return fixed


def _assemble_history(combined):
    hook = combined["hook"]
    hook_text = " ".join(t for t in (hook["claim"], hook["payoff"], hook["curiosity"])
                         if t.strip())
    contract_text = script_gen._contract_beat(combined["segments"])

    body_segments = [{"kind": "body", "title": seg["title"], "text": seg["text"],
                      "reengage": bool(seg.get("reengage")),
                      "short_ready": bool(seg.get("short_ready")),
                      "is_turn": bool(seg.get("is_turn"))}
                     for seg in combined["segments"]]

    # A varied per-video subscribe line, not the fixed channel-wide OUTRO_TEXT
    # (falls back to it only if the model somehow omitted outro_line).
    outro_text = (combined.get("outro_line") or "").strip() or config.channel.OUTRO_TEXT

    hook_scenes = combined.get("hook_scenes") or []
    body_scenes = combined.get("body_scenes") or []
    contract_query = (body_scenes[0]["visual_query"] if body_scenes
                      else "archival newsreel footage")
    outro_query = (body_scenes[-1]["visual_query"] if body_scenes
                  else "archival newsreel footage")
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
        "narrative_shape": combined.get("narrative_shape"),
        "turn": combined.get("turn"),
        "segments": segments,
        "narration": narration,
        "scenes": scenes,
    }


def generate_history_script(topic_title, short=False):
    """Draft + self-critique, then the same hard word-count safety net as the
    generic path. Normal case: 2 Gemini calls (draft, critique). Worst case
    (narration outside hard word-count bounds): 3 - the third is the same
    single targeted expand/trim pass script_gen.py's generic path uses."""
    log.info("generating history script for: %s (mystery/untold lens)", topic_title)
    draft = _generate_draft(topic_title, short)
    combined = _critique(topic_title, draft, short)

    if not short:
        c = config.channel
        words = script_gen._combined_word_count(combined)
        if not (c.SCRIPT_WORDS_HARD_MIN <= words <= c.SCRIPT_WORDS_HARD_MAX):
            log.warning("narration word count %d outside hard bounds [%d, %d]; "
                       "sending one targeted length-fix call", words,
                       c.SCRIPT_WORDS_HARD_MIN, c.SCRIPT_WORDS_HARD_MAX)
            combined = _fix_length_preserving_markers(combined)
            words = script_gen._combined_word_count(combined)
            if not (c.SCRIPT_WORDS_HARD_MIN <= words <= c.SCRIPT_WORDS_HARD_MAX):
                log.warning("still %d words after the length-fix pass; "
                           "accepting closest draft rather than failing", words)

    data = _assemble_history(combined)
    log.info("history script ok: '%s' shape=%s turn=%s",
             data["title"], data.get("narrative_shape"), (data.get("turn") or "")[:80])
    return data

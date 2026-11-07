"""Stage 1 - topic selection.

Default: take the next `pending` topic from the channel's content plan JSON
(config.channel.CONTENT_PLAN, an evergreen queue). Nudge: if pytrends surfaces
a relevant hot topic (per config.channel.TRENDS_CATEGORY_KEYWORDS) AND it
loosely matches a pending topic, that one jumps the queue. If pytrends fails
for any reason, we silently fall back to the queue - the trend check must
never block a run.
"""
import json

from .config import config
from .util import get_logger

log = get_logger("topics")


def _load_plan():
    return json.loads(config.channel.CONTENT_PLAN.read_text())


def _save_plan(plan):
    config.channel.CONTENT_PLAN.write_text(json.dumps(plan, indent=2, ensure_ascii=False))


def _pending(plan):
    return [t for t in plan["topics"] if t["status"] == "pending"]


def _trend_nudge(pending):
    """Return a pending topic that matches a currently trending query for this
    channel's niche, or None. Never raises - trends are a bonus, not a
    dependency."""
    if not config.channel.TRENDS_ENABLED:
        return None
    try:
        from pytrends.request import TrendReq
        py = TrendReq(hl="en-US", tz=0)
        hot_terms = []
        for kw in config.channel.TRENDS_CATEGORY_KEYWORDS:
            py.build_payload([kw], timeframe="now 7-d")
            related = py.related_queries().get(kw, {})
            rising = related.get("rising")
            if rising is not None:
                hot_terms += [q.lower() for q in rising["query"].tolist()]
        if not hot_terms:
            return None
        # Match a hot term against pending topic titles (loose word overlap).
        for t in pending:
            title_words = set(t["title"].lower().split())
            for term in hot_terms:
                if title_words & set(term.split()):
                    log.info("trend nudge: '%s' matches hot term '%s'",
                             t["title"], term)
                    return t
    except Exception as e:  # noqa: BLE001 - trends are strictly optional
        log.warning("trend check skipped (%s)", e)
    return None


def pick_topic(forced_title=None):
    """Return {'id', 'title'} for the topic to make this run.

    forced_title: bypass the queue entirely (used by --topic for testing).
    """
    plan = _load_plan()
    pending = _pending(plan)

    if forced_title:
        log.info("forced topic: %s", forced_title)
        return {"id": None, "title": forced_title}

    if not pending:
        raise RuntimeError(
            "No pending topics left in content_plan.json. Add more topics."
        )

    chosen = _trend_nudge(pending) or pending[0]
    log.info("selected topic #%s: %s", chosen["id"], chosen["title"])
    return {"id": chosen["id"], "title": chosen["title"]}


def mark_published(topic_id, video_id, published_at):
    """Flip a topic to published and record the video id. Skips forced topics
    (id is None)."""
    if topic_id is None:
        return
    plan = _load_plan()
    for t in plan["topics"]:
        if t["id"] == topic_id:
            t["status"] = "published"
            t["video_id"] = video_id
            t["published_at"] = published_at
            break
    _save_plan(plan)
    log.info("marked topic #%s published (%s)", topic_id, video_id)

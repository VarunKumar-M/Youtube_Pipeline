"""Human review gate over Telegram (used for BOTH gates).

Gate 1 (script): text preview + Approve / Edit / Reject.
Gate 2 (video):  thumbnail + short preview clip + Approve / Edit / Reject.

Uses raw Telegram HTTP (requests) so there is no async event loop to clash with
edge-tts. Blocks on long-poll getUpdates until the reviewer acts or it times out.
On timeout it returns {"action": "timeout"} and the pipeline does NOT publish -
silence must never auto-approve.

Telegram bots cannot send files > 50 MB, so the full long-form is watched locally
(output/<run-id>/video.mp4). The gate sends the thumbnail and a short clip only.
"""
import time

import requests

from .config import config
from .util import get_logger

log = get_logger("review")

API = "https://api.telegram.org/bot{token}/{method}"
KEYBOARD = {"inline_keyboard": [[
    {"text": "Approve", "callback_data": "approve"},
    {"text": "Edit", "callback_data": "edit"},
    {"text": "Reject", "callback_data": "reject"},
]]}
POLL_TIMEOUT = 50          # seconds per long-poll call
GATE_TIMEOUT = 3600        # total seconds to wait for a human


def _api(method, files=None, **data):
    url = API.format(token=config.telegram_token, method=method)
    data.setdefault("chat_id", config.telegram_chat_id)
    r = requests.post(url, data=data, files=files, timeout=120)
    r.raise_for_status()
    return r.json()


def _send_message(text, keyboard=False):
    kw = {"text": text[:4000]}
    if keyboard:
        import json as _j
        kw["reply_markup"] = _j.dumps(KEYBOARD)
    return _api("sendMessage", **kw)


def _send_photo(path, caption, keyboard=False):
    kw = {"caption": caption[:1000]}
    if keyboard:
        import json as _j
        kw["reply_markup"] = _j.dumps(KEYBOARD)
    with open(path, "rb") as f:
        return _api("sendPhoto", files={"photo": f}, **kw)


def _send_video(path, caption):
    import os
    if os.path.getsize(path) > 49 * 1_048_576:
        log.info("preview clip too big for Telegram, skipping send")
        return None
    with open(path, "rb") as f:
        return _api("sendVideo", files={"video": f}, caption=caption[:1000])


def _answer_callback(cb_id):
    try:
        _api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception:  # noqa: BLE001
        pass


def _wait_for_action(offset0):
    """Long-poll for a button press or a text edit. Returns (action, feedback)."""
    deadline = time.time() + GATE_TIMEOUT
    offset = offset0
    awaiting_edit_text = False
    while time.time() < deadline:
        resp = _api("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            if "callback_query" in upd:
                cb = upd["callback_query"]
                _answer_callback(cb["id"])
                action = cb.get("data")
                if action == "edit":
                    _send_message("Send your edit as a text message.")
                    awaiting_edit_text = True
                    continue
                return action, None
            msg = upd.get("message", {})
            text = msg.get("text")
            if text:
                # Any text reply is treated as an edit instruction.
                return "edit", text
    return "timeout", None


def _current_offset():
    """Drain old updates so a stale button press does not decide this gate."""
    resp = _api("getUpdates", timeout=0)
    results = resp.get("result", [])
    return (results[-1]["update_id"] + 1) if results else 0


def gate_script(run_id, script):
    log.info("GATE 1: sending script for review")
    offset = _current_offset()
    preview = (f"SCRIPT REVIEW - {run_id}\n\n"
               f"Title: {script['title']}\n\n"
               f"{script['narration'][:1200]}...")
    _send_message(preview, keyboard=True)
    action, feedback = _wait_for_action(offset)
    log.info("GATE 1 decision: %s", action)
    return {"action": action, "feedback": feedback}


def gate_video(run_id, script, thumbnail_path, preview_clip=None):
    log.info("GATE 2: sending video for review")
    offset = _current_offset()
    caption = (f"VIDEO READY - {run_id}\n"
               f"{script['title']}\n\n"
               f"Watch full: output/{run_id}/video.mp4\n"
               f"Approve to publish.")
    _send_photo(thumbnail_path, caption, keyboard=True)
    if preview_clip:
        try:
            _send_video(preview_clip, "Preview (first Short)")
        except Exception as e:  # noqa: BLE001
            log.warning("preview send failed: %s", e)
    action, feedback = _wait_for_action(offset)
    log.info("GATE 2 decision: %s", action)
    return {"action": action, "feedback": feedback}


def notify(text):
    """Heartbeat / status message (no keyboard)."""
    try:
        _send_message(text)
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)

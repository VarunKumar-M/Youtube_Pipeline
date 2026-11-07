"""Shared plumbing: logging, retry/backoff, run directories, and stage caching.

The caching helpers are what make the pipeline idempotent: each stage writes its
output to output/<run-id>/<name>, and a re-run reads the cached file instead of
regenerating. That gives fast edits and crash recovery for free.
"""
import json
import logging
import time
import functools
from datetime import datetime
from pathlib import Path

from .config import config

# --- Logging ---------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def get_logger(name):
    return logging.getLogger(name)


log = get_logger("util")

# --- Retry with exponential backoff ----------------------------------------


def retry(times=3, base_delay=2.0, exceptions=(Exception,)):
    """Retry a function with exponential backoff. Wrap every external call."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    if attempt == times:
                        log.error("%s failed after %d attempts: %s",
                                  fn.__name__, times, e)
                        raise
                    log.warning("%s attempt %d/%d failed (%s); retrying in %.0fs",
                                fn.__name__, attempt, times, e, delay)
                    time.sleep(delay)
                    delay *= 2
        return wrapper
    return decorator

# --- Run directories -------------------------------------------------------


def new_run_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_dir(run_id):
    d = config.channel_output_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d

# --- Stage caching ---------------------------------------------------------


def stage_path(run_id, name):
    return run_dir(run_id) / name


def cache_exists(run_id, name):
    return stage_path(run_id, name).exists()


def save_json(run_id, name, data):
    path = stage_path(run_id, name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log.info("saved %s", path.name)
    return path


def load_json(run_id, name):
    return json.loads(stage_path(run_id, name).read_text())

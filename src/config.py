"""Loads environment + settings and exposes them as a single object.

Keeps every path and key in one place so modules never touch os.environ directly.

Channel selection: call config.set_channel(slug) ONCE, at the very start of
run.py's main() before any pipeline/publish code runs, then read
config.channel.X for anything channel-specific (identity, OAuth paths, output
dir, thumbnail palette, word-count bounds, topics, outro, voices). No function
signature anywhere should default a parameter to a config.channel.X
expression - Python evaluates defaults at import time, before a channel has
been selected.
"""
import importlib
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Project root = one level above src/
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CHANNELS_DIR = CONFIG_DIR / "channels"
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"

# Make `config/settings.py` importable regardless of where run.py is called from.
sys.path.insert(0, str(CONFIG_DIR))
import settings  # noqa: E402

load_dotenv(ROOT / ".env")


def _get(name, required=True, default=None):
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing {name}. Copy .env.example to .env and fill it in."
        )
    return val


def list_channels():
    """Slugs of every channel config module under config/channels/."""
    if not CHANNELS_DIR.exists():
        return []
    return sorted(p.stem for p in CHANNELS_DIR.glob("*.py") if p.stem != "__init__")


class Config:
    def __init__(self):
        self.root = ROOT
        self.output_dir = OUTPUT_DIR
        self.assets_dir = ASSETS_DIR
        self.settings = settings
        self.channel = None
        self.channel_slug = None

        # Keys are loaded lazily via the helpers below so that, e.g., the script
        # stage does not fail just because the YouTube file is absent.

    def set_channel(self, slug):
        valid = list_channels()
        if slug not in valid:
            raise RuntimeError(
                f"Unknown channel '{slug}'. Valid channels: "
                f"{', '.join(valid) or '(none configured)'}"
            )
        sys.path.insert(0, str(CHANNELS_DIR))
        self.channel = importlib.import_module(slug)
        self.channel_slug = slug
        return self.channel

    @property
    def channel_output_dir(self):
        if self.channel is None:
            raise RuntimeError("No channel selected - call config.set_channel() first.")
        return self.output_dir / self.channel_slug

    @property
    def gemini_key(self):
        return _get("GEMINI_API_KEY")

    @property
    def pixabay_key(self):
        return _get("PIXABAY_API_KEY")

    @property
    def pexels_key(self):
        # Optional: Pixabay is the fallback, so this one is allowed to be empty.
        return _get("PEXELS_API_KEY", required=False)

    @property
    def telegram_token(self):
        return _get("TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self):
        return _get("TELEGRAM_CHAT_ID")


config = Config()

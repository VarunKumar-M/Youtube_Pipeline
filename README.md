# AutoYouTube

An automated pipeline that generates faceless long-form finance videos and Shorts,
with **two human review gates** before anything is published.

The gates are the point of this project: nothing goes public without a human tap on
Telegram. That is what keeps it compliant and what makes it a real engineering piece
rather than a spam bot.

## Flow

```
trigger (cron or manual)
  -> pick topic            content_plan.json + optional pytrends nudge
  -> generate script       Gemini
        --> GATE 1: approve / edit / reject   (cheap, before any render)
  -> voice + timings        edge-tts (WordBoundary events; no Whisper needed)
  -> captions               Pillow PNG overlays (no ImageMagick needed)
  -> visuals                Pixabay video (Pexels fallback)
  -> assembly               MoviePy -> video.mp4
  -> cut 2 Shorts           from the finished long-form
  -> thumbnail              template + frame from the video
        --> GATE 2: approve / edit / reject   (after render)
  -> publish long-form now  YouTube Data API
  -> schedule 2 Shorts      private + publishAt, staggered +6h / +12h (no gate)
  -> mark topic done + Telegram heartbeat
```

## Build status - ALL STAGES WRITTEN

- [x] Stage 1 - topic selection
- [x] Stage 2 - script generation (Gemini)
- [x] Stage 3+4 - voice + word timings (edge-tts, replaces Whisper)
- [x] Stage 4 - caption rendering (Pillow)
- [x] Stage 5 - visuals (Pixabay / Pexels)
- [x] Stage 6 - assembly (MoviePy)
- [x] Stage 7 - Shorts cutting
- [x] Stage 8 - thumbnail
- [x] Gate - Telegram review (both gates)
- [x] Stage 9 - YouTube upload + scheduled Shorts

Written and byte-compile clean. NOT yet run end-to-end - that is your first test,
since it needs the real keys, network, and installed packages.

## Setup

1. Python **3.11** (moviepy 1.0.3 is happiest here)
2. `pip install -r requirements.txt`  (installs a bundled ffmpeg via imageio-ffmpeg)
3. `cp .env.example .env` and fill in the keys
4. Put your OAuth `client_secret.json` in the project root

### Keys needed (all free)

| Key | Where |
|-----|-------|
| `GEMINI_API_KEY` | aistudio.google.com -> Get API key |
| `PIXABAY_API_KEY` | pixabay.com/api/docs (shown when logged in) |
| `PEXELS_API_KEY` | pexels.com/api (optional; Pixabay is the fallback) |
| `TELEGRAM_BOT_TOKEN` | BotFather -> /newbot |
| `TELEGRAM_CHAT_ID` | @userinfobot |
| `client_secret.json` | Google Cloud -> OAuth desktop client (file, not env) |

## Run

```bash
# fastest first test: script only, ~130-word narration
python run.py --stage script --short

# then chain later stages against that run (use the printed run id)
python run.py --stage voice     --run-id 20260819-101500
python run.py --stage assembly  --run-id 20260819-101500 --short

# full pipeline, everything except the real upload
python run.py --short --dry-run

# full pipeline, real upload but as a private video (safe live test)
python run.py --short --privacy private

# real run
python run.py
```

Every stage caches its output under `output/<run-id>/`, so re-running a later
stage does not regenerate earlier ones. That is what makes edits fast and crashes
recoverable, and it is what lets `--stage` work.

## Notes / decisions baked in

- **No Whisper.** edge-tts gives exact word timings for free, so captions are
  exact and there is no 100MB+ model download or CPU transcription.
- **moviepy pinned to 1.0.3** with **Pillow<10** and **numpy<2** on purpose -
  see the comment in requirements.txt.
- **Shorts auto-publish** (no third gate), scheduled +6h/+12h after the long-form.
- **A/S disclosure** (`containsSyntheticMedia`) defaults to True; see
  `config/settings.py` for the nuance on whether it is actually required.
- Reject guard: max 3 reject loops per run, then it aborts instead of burning quota.
- Gate timeout does NOT auto-publish - silence aborts the run.

## Cron

Cron just calls the same command you run by hand, e.g. once a day:

```
0 6 * * *  cd /path/to/autoyoutube && /path/to/python run.py >> run.log 2>&1
```

#!/usr/bin/env python3
"""AutoYouTube entry point.

Examples:
  python run.py --channel clearcents                       # full pipeline
  python run.py --channel clearcents --stage script         # one stage
  python run.py --channel clearcents --short                # 60s test video
  python run.py --channel clearcents --dry-run              # no real upload
  python run.py --channel clearcents --topic "..."          # force a topic
  python run.py --channel clearcents --publish --run-id ID  # upload an
                                                              # already-built run, no Telegram
"""
import argparse
import sys

from src.config import config, list_channels
from src.pipeline import run, publish_existing, STAGES


def main():
    p = argparse.ArgumentParser(description="AutoYouTube pipeline")
    p.add_argument("--channel", default=None,
                   help="which channel config to run (required) - see config/channels/")
    p.add_argument("--stage", choices=STAGES,
                   help="run only this stage using cached earlier output")
    p.add_argument("--short", action="store_true",
                   help="short test video (~60s) instead of full length")
    p.add_argument("--dry-run", action="store_true",
                   help="do everything except the real YouTube upload")
    p.add_argument("--topic", help="force a specific topic title")
    p.add_argument("--run-id", help="reuse an existing run id (for --stage)")
    p.add_argument("--privacy", default=None,
                   choices=["public", "unlisted", "private"],
                   help="long-form privacy (default: settings.LONGFORM_DEFAULT_PRIVACY)")
    p.add_argument("--publish", action="store_true",
                   help="upload an already-built --run-id's video + Shorts "
                        "directly, skipping both Telegram gates entirely")
    args = p.parse_args()

    # No argparse `choices=` here on purpose - this gives full control over
    # the message for both the "missing" and "unknown" cases, rather than
    # argparse's generic phrasing for just one of them.
    valid = list_channels()
    if not args.channel:
        print(f"\n[error] --channel is required. Valid channels: {', '.join(valid)}")
        sys.exit(1)
    if args.channel not in valid:
        print(f"\n[error] unknown channel '{args.channel}'. "
              f"Valid channels: {', '.join(valid)}")
        sys.exit(1)
    config.set_channel(args.channel)

    if args.publish and not args.run_id:
        print("\n[error] --publish requires --run-id")
        sys.exit(1)

    privacy = args.privacy or config.settings.LONGFORM_DEFAULT_PRIVACY

    try:
        if args.publish:
            vid = publish_existing(args.run_id, privacy=privacy)
            print(f"\npublished: https://youtu.be/{vid}")
            print(f"output: output/{args.channel}/{args.run_id}/")
            return

        run_id = run(only=args.stage, short=args.short, dry_run=args.dry_run,
                     forced_topic=args.topic, run_id=args.run_id,
                     privacy=privacy)
        print(f"\nrun id: {run_id}")
        print(f"output: output/{args.channel}/{run_id}/")
    except NotImplementedError as e:
        print(f"\n[not built yet] {e}")
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print(f"\n[error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

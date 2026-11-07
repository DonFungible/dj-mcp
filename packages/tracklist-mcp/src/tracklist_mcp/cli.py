"""`tracklist <youtube link>`: print the tracklist of a DJ mix."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import pipeline
from .audio import AudioError
from .recognize import RecognizerUnavailable
from .timeline import render


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("source", help="YouTube (or SoundCloud, Mixcloud, ...) link, or a local audio file")
    ap.add_argument("--hop", type=float, default=60.0, help="seconds between first-pass probes of the mix (default 60)")
    ap.add_argument("--fast", action="store_true", help="coarser probing and no lyrics; quicker, may miss short tracks")
    ap.add_argument("--no-stems", action="store_true", help="don't separate vocals (no acapella detection)")
    ap.add_argument("--no-lyrics", action="store_true", help="don't transcribe vocals that nothing matched")
    ap.add_argument(
        "--no-versions", action="store_true", help="don't search YouTube for the exact remix Shazam doesn't know"
    )
    ap.add_argument("--keep-audio", action="store_true", help="keep the downloaded audio and stems")
    ap.add_argument("--refresh", action="store_true", help="ignore a saved tracklist for this mix and redo it")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")


def options_from(args: argparse.Namespace) -> pipeline.Options:
    return pipeline.Options(
        hop=120.0 if args.fast else args.hop,
        min_step=15.0 if args.fast else 7.5,
        vocal_hop=60.0 if args.fast else 30.0,
        stems=not args.no_stems,
        lyrics=not (args.no_lyrics or args.fast),
        keep_audio=args.keep_audio,
        find_versions=not args.no_versions,
    )


def run(args: argparse.Namespace) -> int:
    result = None if args.refresh else pipeline.load_result(args.source)
    if result is None:
        t0 = time.monotonic()

        def progress(msg: str) -> None:
            el = int(time.monotonic() - t0)
            print(f"[{el // 60:02d}:{el % 60:02d}] {msg}", file=sys.stderr, flush=True)

        try:
            result = pipeline.run(args.source, options_from(args), progress)
        except (AudioError, RecognizerUnavailable) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nStopped. Clips identified so far are saved; run the same command to continue.", file=sys.stderr)
            return 130
        print(file=sys.stderr)

    if args.json:
        print(json.dumps(result.to_dict(), indent=1))
    else:
        print(render(result.entries, result.info.title, result.info.duration))
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tracklist", description="Make a timestamped tracklist from a DJ mix.")
    add_arguments(ap)
    sys.exit(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()

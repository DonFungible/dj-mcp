"""MCP server: paste a DJ mix link, get a timestamped tracklist."""

from __future__ import annotations

import argparse
import threading
import time
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import cli, pipeline
from .audio import AudioError, mix_id
from .recognize import RecognizerUnavailable
from .timeline import render

mcp = MCPServer(
    "tracklist",
    instructions=(
        "Make a timestamped tracklist from a DJ mix link (YouTube, SoundCloud, Mixcloud) or a local audio file. "
        "identify_mix starts the work in the background and returns at once; call get_tracklist with the same "
        "link to check progress and get the result. A one-hour mix takes roughly 10-30 minutes, mostly because "
        "Shazam only accepts a few lookups a minute. The tracklist marks overlapping tracks, acapellas laid "
        "over other tracks, and stretches nothing matched. For 'unidentified vocal' entries it includes "
        "transcribed lyrics: use them to suggest what the song might be, and say it's a guess."
    ),
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


class Job:
    def __init__(self, source: str, options: pipeline.Options):
        self.source = source
        self.options = options
        self.log: list[str] = []
        self.started = time.time()
        self.result: pipeline.Result | None = None
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = pipeline.run(self.source, self.options, self.log.append)
        except (AudioError, RecognizerUnavailable) as e:
            self.error = str(e)
        except Exception as e:  # report anything else to the client instead of dying silently
            self.error = f"{type(e).__name__}: {e}"

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _payload(result: pipeline.Result) -> dict[str, Any]:
    return {
        "status": "done",
        "tracklist": render(result.entries, result.info.title, result.info.duration),
        "entries": [
            {k: v for k, v in asdict(e).items() if v not in (None, [], {}, "")} | {"confidence": e.confidence}
            for e in result.entries
        ],
        "mix": {"title": result.info.title, "url": result.info.url, "duration_seconds": round(result.info.duration)},
        "uploader_description": (result.info.description or "")[:1500] or None,
        "stats": result.stats,
    }


@mcp.tool(annotations=READ)
def identify_mix(
    url: str,
    hop_seconds: float = 60.0,
    find_acapellas: bool = True,
    transcribe_lyrics: bool = True,
    redo: bool = False,
) -> dict[str, Any]:
    """Start making a tracklist for a DJ mix. Returns immediately; poll get_tracklist(url) for progress and the result.

    url: YouTube/SoundCloud/Mixcloud link or a local audio file path.
    hop_seconds: first-pass sampling interval. Transitions are then narrowed down to ~8 s, so 60 is a good
        default; 120 is quicker but can miss a track played for under two minutes.
    find_acapellas: separate the vocals to find acapellas and layered tracks (adds a few minutes).
    transcribe_lyrics: transcribe vocals nothing matched, so they can be looked up by lyrics.
    redo: ignore a saved tracklist for this mix.
    """
    if not redo and (saved := pipeline.load_result(url)):
        return _payload(saved)
    key = mix_id(url)
    with _lock:
        job = _jobs.get(key)
        if job and job.running:
            return {"status": "running", "progress": job.log[-3:], "note": "Already working on this mix."}
        opts = pipeline.Options(hop=hop_seconds, stems=find_acapellas, lyrics=transcribe_lyrics)
        job = _jobs[key] = Job(url, opts)
        job.thread.start()
    return {"status": "started", "note": "Call get_tracklist with the same url in a minute or two to check on it."}


@mcp.tool(annotations=READ)
def get_tracklist(url: str) -> dict[str, Any]:
    """Progress or result of identify_mix for this link. When done, includes the formatted tracklist and entries."""
    job = _jobs.get(mix_id(url))
    if job and job.running:
        return {
            "status": "running",
            "elapsed_seconds": round(time.time() - job.started),
            "progress": job.log[-4:],
        }
    if job and job.error:
        raise ToolError(job.error)
    if saved := pipeline.load_result(url):
        return _payload(saved)
    raise ToolError("No tracklist for this link yet. Call identify_mix first.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tracklist-mcp", description="DJ mix tracklist MCP server")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    cli.add_arguments(sub.add_parser("identify", help="make a tracklist in the terminal"))
    args = ap.parse_args(argv)
    if args.command == "identify":
        raise SystemExit(cli.run(args))
    mcp.run()


if __name__ == "__main__":
    main()

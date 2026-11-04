"""Cue points, beatgrid and Mixed In Key data read from audio files, cached by modification time."""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .models import Track
from .tags import FileAnalysis, read_file_analysis

_cache: dict[str, tuple[int, FileAnalysis | None]] = {}
_lock = threading.Lock()


def analyze(track: Track) -> FileAnalysis | None:
    if track.missing:
        return None
    try:
        mtime = os.stat(track.path).st_mtime_ns
    except OSError:
        return None
    with _lock:
        hit = _cache.get(track.path)
    if hit and hit[0] == mtime:
        return hit[1]
    analysis: FileAnalysis | None = None
    try:
        analysis = read_file_analysis(track.path)
    except Exception as err:  # noqa: BLE001 - a bad tag shouldn't break a whole set plan
        print(f"serato-mcp: could not read tags of {track.path}: {err}", file=sys.stderr)
    with _lock:
        _cache[track.path] = (mtime, analysis)
    return analysis


def analyze_many(tracks: list[Track], workers: int = 16) -> dict[str, FileAnalysis | None]:
    if not tracks:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(tracks))) as pool:
        results = list(pool.map(analyze, tracks))
    return {t.path: a for t, a in zip(tracks, results, strict=True)}

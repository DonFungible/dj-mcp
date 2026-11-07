"""Identifying clips with Shazam, at a pace Shazam accepts.

Shazam answers a burst of requests, then `429` with no `Retry-After`. How many it allows varies: a fresh client
got about 20 a minute, one that had been busy for a while got 3 every 40 seconds or so. The client spaces
requests out, slows down each time it's refused and speeds back up after a run of successes, and caches every
answer on disk so a rerun or a crash never asks twice.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientError
from aiohttp_retry import ExponentialRetry
from shazamio import HTTPClient, Shazam
from shazamio.exceptions import BadResponseStatus, FailedDecodeJson, RateLimited


@dataclass(frozen=True)
class Match:
    key: str
    title: str
    artist: str
    offset: float  # where the clip sits in the original recording, in seconds
    timeskew: float  # how much faster/slower than the original the clip plays
    url: str | None = None
    isrc: str | None = None

    @classmethod
    def from_response(cls, r: dict[str, Any]) -> Match | None:
        track = r.get("track")
        matches = r.get("matches") or []
        if not track or not matches:
            return None
        m = matches[0]
        return cls(
            key=str(track.get("key", "")),
            title=track.get("title") or "?",
            artist=track.get("subtitle") or "?",
            offset=float(m.get("offset") or 0.0),
            timeskew=float(m.get("timeskew") or 0.0),
            url=track.get("url"),
            isrc=track.get("isrc"),
        )


class RecognizerUnavailable(RuntimeError):
    pass


class Recognizer:
    def __init__(self, cache_file: Path | None = None, min_interval: float = 2.8, max_wait: float = 900.0):
        self.cache_file = cache_file
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.cache: dict[str, dict | None] = {}
        if cache_file and cache_file.exists():
            self.cache = json.loads(cache_file.read_text())
        self._last = 0.0
        self._streak = 0
        self._floor = min_interval
        self._dirty = 0
        self.requests = 0
        self.throttled = 0
        self._shazam: Shazam | None = None
        self._http: HTTPClient | None = None

    def _client(self) -> Shazam:
        if self._shazam is None:
            # Retry server errors, but let 429 through so the pacing below handles it.
            self._http = HTTPClient(retry_options=ExponentialRetry(attempts=3, statuses={500, 502, 503, 504}))
            self._shazam = Shazam(http_client=self._http)
        return self._shazam

    def cached(self, key: str) -> bool:
        return key in self.cache

    def lookup(self, key: str) -> Match | None:
        hit = self.cache.get(key)
        return Match(**hit) if hit else None

    async def identify(self, key: str, wav: bytes) -> Match | None:
        if key in self.cache:
            return self.lookup(key)
        shazam = self._client()
        backoff = 15.0
        waited = 0.0
        failures = 0
        while True:
            pause = self._last + self.min_interval - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
            self._last = time.monotonic()
            self.requests += 1
            try:
                answer = await asyncio.wait_for(shazam.recognize(wav), timeout=30)
                break
            except RateLimited:
                self.throttled += 1
                if waited >= self.max_wait:
                    raise RecognizerUnavailable(
                        "Shazam kept refusing requests (rate limited) for 15 minutes. Wait a while and run it again;"
                        " finished clips are saved, so it picks up where it stopped."
                    ) from None
                self._streak = 0
                self.min_interval = min(self.min_interval * 1.5, 20.0)
                await asyncio.sleep(backoff)
                waited += backoff
                backoff = min(backoff * 1.6, 120.0)
            except (ClientError, TimeoutError, BadResponseStatus, FailedDecodeJson) as e:
                failures += 1
                if failures >= 4:
                    raise RecognizerUnavailable(f"Couldn't reach Shazam: {type(e).__name__}: {e}") from e
                await asyncio.sleep(5 * failures)

        self._streak += 1
        if self._streak >= 8:
            self._streak = 0
            self.min_interval = max(self._floor, self.min_interval * 0.85)

        match = Match.from_response(answer)
        self.cache[key] = asdict(match) if match else None
        self._dirty += 1
        if self._dirty >= 10:
            self.save()
        return match

    def save(self) -> None:
        if self.cache_file and self._dirty:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache))
            tmp.replace(self.cache_file)
            self._dirty = 0

    async def close(self) -> None:
        self.save()
        if self._http is not None:
            await self._http.close()
        self._shazam = self._http = None

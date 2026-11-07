"""Link in, tracklist out.

1. Download the mix and split it into a vocal stem and an instrumental stem (Demucs).
2. Fingerprint the full mix every `hop` seconds. Between two probes that heard the same record playing straight
   through (same song, and its position advanced by the time between them) nothing changed; elsewhere, probe
   halfway and repeat, down to `min_step`. Shazam allows few lookups a minute, so this matters.
3. Fingerprint the vocal stem wherever someone is singing, the same way, which finds acapellas over other tracks.
4. Fingerprint the instrumental stem where the vocal stem and the mix disagree, to find what's underneath.
5. Transcribe vocals nothing matched, so they can be looked up by their lyrics.
6. Join it all into plays that can overlap (timeline.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np

from . import audio, verify, versions
from .recognize import Match, Recognizer
from .timeline import INSTRUMENTAL, MIX, VOCALS, Entry, Probe, build, continuous, number, song_id

Progress = Callable[[str], None]


@dataclass
class Options:
    hop: float = 60.0  # seconds between first-pass probes of the full mix
    min_step: float = 7.5  # finest spacing when narrowing down a transition
    vocal_hop: float = 30.0  # seconds between first-pass probes of the vocal stem
    window: float = 12.0  # seconds of audio per probe
    stems: bool = True  # separate vocals to find acapellas and layered tracks
    lyrics: bool = True  # transcribe vocals that nothing matched
    keep_audio: bool = False  # keep the downloaded audio and stems after finishing
    find_versions: bool = True  # name remixes Shazam doesn't know by comparing YouTube uploads with the mix
    vocal_ratio: float = 0.12  # vocal stem at least this loud relative to the mix counts as singing


@dataclass
class Result:
    info: audio.MixInfo
    entries: list[Entry]
    stats: dict

    def to_dict(self) -> dict:
        return {"mix": asdict(self.info), "entries": [asdict(e) for e in self.entries], "stats": self.stats}

    @classmethod
    def from_dict(cls, d: dict) -> Result:
        entries = []
        for e in d["entries"]:
            entries.append(Entry(**e))
        return cls(info=audio.MixInfo(**d["mix"]), entries=entries, stats=d["stats"])


def result_path(source: str) -> Path:
    return audio.cache_root() / audio.mix_id(source) / "result.json"


def load_result(source: str) -> Result | None:
    """The saved tracklist for a mix, rebuilt from its saved Shazam answers so timeline fixes apply to it."""
    p = result_path(source)
    if not p.exists():
        return None
    result = Result.from_dict(json.loads(p.read_text()))
    matches = p.parent / "matches.json"
    if matches.exists():
        opt = Options(**result.stats.get("options", {}))
        vocal = [e for e in result.entries if e.role == "unidentified vocal"]
        beds = verify.load(p.parent / "beds.json")
        found = versions.load(p.parent / "versions.json")["found"]
        probes = probes_from_cache(json.loads(matches.read_text()))
        result.entries = assemble(probes, result.info.duration, opt, vocal, beds, found, result.info.description)
    return result


def probes_from_cache(cache: dict[str, dict | None]) -> list[Probe]:
    out = []
    for key, hit in cache.items():
        source_name, t, _window = key.split(":")
        out.append(Probe(float(t), source_name, Match(**hit) if hit else None))
    return out


def assemble(
    probes: list[Probe],
    duration: float,
    opt: Options,
    extra: list[Entry] = (),
    beds: list | None = None,
    found: list | None = None,
    description: str | None = None,
) -> list[Entry]:
    entries = build(
        probes, duration, opt.window, hop=opt.min_step * 2, max_gap=max(130.0, opt.hop * 2.2), beds=beds or []
    )
    entries = entries + list(extra)
    versions.apply(entries, found or [])
    versions.orient(entries, description)
    return number(entries)


def run(source: str, options: Options | None = None, progress: Progress = lambda _: None) -> Result:
    return asyncio.run(run_async(source, options or Options(), progress))


async def run_async(source: str, opt: Options, progress: Progress) -> Result:
    started = time.monotonic()
    work = audio.cache_root() / audio.mix_id(source)
    info = await asyncio.to_thread(audio.fetch, source, work, progress)
    files = {MIX: work / "mix.flac", VOCALS: work / "vocals.flac", INSTRUMENTAL: work / "instrumental.flac"}

    if opt.stems:
        await asyncio.to_thread(audio.separate, work, progress)

    rec = Recognizer(work / "matches.json")
    beds = verify.load(work / "beds.json")
    probes: dict[tuple[str, float], Probe] = {}

    async def probe(source_name: str, t: float) -> Match | None:
        t = round(t, 1)
        if (source_name, t) in probes:
            return probes[(source_name, t)].match
        key = f"{source_name}:{t}:{opt.window}"
        wav = await asyncio.to_thread(audio.clip, files[source_name], t, opt.window)
        m = await rec.identify(key, wav)
        probes[(source_name, t)] = Probe(t, source_name, m)
        return m

    counter = {"n": 0, "last": time.monotonic()}

    def tick(label: str) -> None:
        counter["n"] += 1
        if time.monotonic() - counter["last"] > 20:
            counter["last"] = time.monotonic()
            progress(f"{label}: {counter['n']} clips checked")

    async def sweep(label: str, source_name: str, times: list[float]) -> None:
        todo = [t for t in times if (source_name, round(t, 1)) not in probes]
        fresh = sum(not rec.cached(f"{source_name}:{round(t, 1)}:{opt.window}") for t in todo)
        if fresh:
            progress(f"{label}: {len(todo)} clips")
        for t in todo:
            await probe(source_name, t)
            tick(label)

    def sid(m: Match | None) -> str | None:
        return song_id(m.title) if m else None

    async def refine(label: str, source_name: str, ta: float, tb: float, min_step: float | None = None) -> None:
        """Probe halfway between ta and tb until each half is continuous or shorter than min_step."""
        min_step = min_step or opt.min_step
        ma, mb = await probe(source_name, ta), await probe(source_name, tb)
        if tb - ta <= min_step * 1.01 or continuous(ta, ma, tb, mb):
            return
        mid = round((ta + tb) / 2, 1)
        await probe(source_name, mid)
        tick(label)
        await refine(label, source_name, ta, mid, min_step)
        await refine(label, source_name, mid, tb, min_step)

    async def confirm(label: str) -> None:
        """Sample 12 s either side of each match that isn't confirmed yet.

        Megamixes and quick-mixing DJs play records for a minute or less, so the first pass may hear one only
        once. A record really playing shows its position advancing by those 12 s; a stray loop doesn't.
        """
        entries = build(list(probes.values()), info.duration, opt.window, hop=opt.min_step * 2)
        todo: list[tuple[str, float]] = []
        for e in entries:
            if e.confirmed or e.role.startswith("unidentified"):
                continue
            own = song_id(e.title)
            first = next(
                (p for p in sorted(probes.values(), key=lambda p: p.t) if p.match and sid(p.match) == own
                 and e.start - opt.window <= p.t <= e.end + opt.window),
                None,
            )  # fmt: skip
            if first is None:
                continue
            for dt in (12.0, -12.0):
                t = round(first.t + dt, 1)
                if 0 <= t <= end and (first.source, t) not in probes and (first.source, t) not in todo:
                    todo.append((first.source, t))
        if todo:
            progress(f"{label}: {len(todo)} clips")
        for source_name, t in todo[:80]:
            await probe(source_name, t)
            tick(label)

    try:
        end = max(info.duration - opt.window, 0.1)
        grid = [float(t) for t in np.arange(0, end, opt.hop)] + [round(end, 1)]

        await sweep("Identifying the mix", MIX, grid)
        progress("Refining transitions")
        for ta, tb in pairwise(grid):
            await refine("Refining transitions", MIX, ta, tb)
        await confirm("Confirming short plays")

        if opt.stems:
            mix_level = audio.loudness(files[MIX])
            voc_level = audio.loudness(files[VOCALS])

            def singing(t: float) -> bool:
                a, b = int(t), int(t + opt.window)
                v, m = voc_level[a:b], mix_level[a:b]
                return bool(len(v)) and float(v.mean()) >= opt.vocal_ratio * max(float(m.mean()), 1e-4)

            vgrid = [float(t) for t in np.arange(0, end, opt.vocal_hop) if singing(float(t))]
            await sweep("Checking vocals for acapellas", VOCALS, vgrid)
            progress("Refining acapellas")
            for ta, tb in pairwise(vgrid):
                if tb - ta <= opt.vocal_hop * 1.01:
                    await refine("Refining acapellas", VOCALS, ta, tb)

            # Wherever a vocal was recognised, check what's under it. A loud acapella can dominate the full mix
            # too, so only the instrumental stem tells a vocal track apart from an acapella over another record.
            # Check at the first-pass vocal probes and the first-pass mix probes of songs that have vocals, then
            # narrow down wherever the instrumental changes.
            sung = {sid(p.match) for (s, _), p in probes.items() if s == VOCALS and p.match is not None}
            under = {t for t in vgrid if probes[(VOCALS, round(t, 1))].match is not None}
            under |= {t for t in grid if sid(probes[(MIX, round(t, 1))].match) in sung}
            under_times = sorted(under)
            await sweep("Checking what's under the vocals", INSTRUMENTAL, under_times)
            # Narrow down only where two named records meet: Shazam often can't name an instrumental at all,
            # and chasing every "unrecognised" boundary costs lookups without finding anything.
            for ta, tb in pairwise(under_times):
                ma, mb = probes[(INSTRUMENTAL, round(ta, 1))].match, probes[(INSTRUMENTAL, round(tb, 1))].match
                if tb - ta <= opt.hop * 1.01 and ma and mb and sid(ma) != sid(mb):
                    await refine("Checking what's under the vocals", INSTRUMENTAL, ta, tb, opt.min_step * 2)

            await confirm("Confirming short acapellas")

            # Where Shazam couldn't name the music under a vocal, compare it with the vocal's released record,
            # every 6 s across the stretch that vocal is heard, so one odd window can't decide it.
            unexplained = {
                p.match.key: p.match
                for (s, t), p in probes.items()
                if s == VOCALS
                and p.match is not None
                and (INSTRUMENTAL, t) in probes
                and probes[(INSTRUMENTAL, t)].match is None
            }
            done = {(round(r["t"], 1), r["key"]) for r in beds}
            windows = []
            for key, m in unexplained.items():
                # The stretches this song is heard in (any source), each sampled every 6 s.
                own = sid(m)
                heard = sorted(t for (_, t), p in probes.items() if p.match and sid(p.match) == own)
                stretches = [[heard[0], heard[0]]]
                for t in heard[1:]:
                    if t - stretches[-1][1] > 130:
                        stretches.append([t, t])
                    stretches[-1][1] = t
                for a, b in stretches:
                    for t in np.arange(a, b + 0.1, 6.0):
                        t = round(float(t), 1)
                        if (t, key) not in done:
                            windows.append((t, m))
            if windows:
                progress(f"Comparing the music under {len(unexplained)} vocal(s) with the released records")
                beds += await asyncio.to_thread(
                    verify.check_beds, windows, files[INSTRUMENTAL], opt.window, work / "refs"
                )
                verify.save(work / "beds.json", beds)
    finally:
        await rec.close()

    all_probes = list(probes.values())
    entries = assemble(all_probes, info.duration, opt, beds=beds)

    vocal_entries: list[Entry] = []
    if opt.stems and opt.lyrics:
        vocal_entries = await asyncio.to_thread(_unmatched_vocals, all_probes, entries, files[VOCALS], opt, progress)
        entries = assemble(all_probes, info.duration, opt, vocal_entries, beds)

    found: list[dict] = []
    if opt.find_versions:
        found = await asyncio.to_thread(_find_versions, entries, files[MIX], work, info, progress)
        entries = assemble(all_probes, info.duration, opt, vocal_entries, beds, found, info.description)

    stats = {
        "probes": len(all_probes),
        "shazam_requests": rec.requests,
        "rate_limited": rec.throttled,
        "seconds": round(time.monotonic() - started),
        "options": asdict(opt),
    }
    result = Result(info=info, entries=entries, stats=stats)
    (work / "result.json").write_text(json.dumps(result.to_dict(), indent=1))
    if not opt.keep_audio:
        audio.cleanup(work)
    return result


def _find_versions(entries: list[Entry], mix: Path, work: Path, info: audio.MixInfo, progress: Progress) -> list[dict]:
    """Check each track is the version Shazam named; if not, look for the remix on YouTube. Saved per track."""
    store = versions.load(work / "versions.json")
    checked = set(store["checked"])
    todo = [
        e
        for e in entries
        if e.confirmed and e.role in ("track", "instrumental") and f"{song_id(e.title)}@{round(e.start)}" not in checked
    ]
    for i, e in enumerate(todo, 1):
        progress(f"Checking which version of each track was played ({i}/{len(todo)}): {e.title}")
        try:
            hints = versions.hints_for(e, info.title, info.description)
            tried: list[str] = []
            listed = versions.listed_versions(e, info.description)
            if v := versions.find_version(e, mix, work / "versions", hints, tried, listed):
                store["found"].append(v)
            store.setdefault("tried", {})[f"{song_id(e.title)}@{round(e.start)}"] = tried
        except Exception:  # a failed lookup leaves Shazam's name in place
            pass
        store["checked"].append(f"{song_id(e.title)}@{round(e.start)}")
        versions.save(work / "versions.json", store)

    # Stretches nothing confirmed covers: what does the uploader's own tracklist say plays there?
    if versions.listing(info.description):
        named = {song_id(e.title) for e in entries if e.confirmed} | {v["song"] for v in store["found"]}
        targets = [
            e
            for e in entries
            if (e.role.startswith("unidentified") or not e.confirmed) and f"gap@{round(e.start)}" not in checked
        ]
        if targets:
            progress(f"Checking {len(targets)} unidentified stretch(es) against the uploader's tracklist")
            with contextlib.suppress(Exception):  # a failed lookup leaves the stretch as it was
                flipped = versions.title_first(entries, info.description)
                store["found"] += versions.fill_from_listing(
                    targets, named, mix, work / "versions", info.description, flipped
                )
            store["checked"] += [f"gap@{round(e.start)}" for e in targets]
            versions.save(work / "versions.json", store)
    return store["found"]


def _unmatched_vocals(
    probes: list[Probe], entries: list[Entry], vocals: Path, opt: Options, progress: Progress
) -> list[Entry]:
    """Runs of vocal probes nothing matched, that no identified acapella or vocal track explains."""
    misses = sorted(p.t for p in probes if p.source == VOCALS and p.match is None)
    vocal_hits = {(p.t, song_id(p.match.title)) for p in probes if p.source == VOCALS and p.match is not None}
    # A record explains a vocal only if its own vocal was heard during that play, not in some earlier play.
    explained = [
        e
        for e in entries
        if e.role in ("acapella", "track")
        and any(s == song_id(e.title) and e.start - opt.window <= vt <= e.end for vt, s in vocal_hits)
    ]

    def covered(t: float) -> bool:
        return any(e.start <= t + opt.window / 2 <= e.end for e in explained)

    runs: list[list[float]] = []
    for t in misses:
        if covered(t):
            continue
        if runs and t - runs[-1][-1] <= opt.vocal_hop * 1.01:
            runs[-1].append(t)
        else:
            runs.append([t])
    runs = [r for r in runs if len(r) >= 2][:30]
    if not runs:
        return []

    model = _whisper()
    if model is None:
        return []
    out = []
    for i, r in enumerate(runs, 1):
        progress(f"Transcribing unmatched vocals ({i}/{len(runs)})")
        start, end = r[0], min(r[-1] + opt.window, r[0] + 45)
        text = _transcribe(model, audio.read_seconds(vocals, start, end - start))
        if text:
            out.append(
                Entry(
                    start=start, end=r[-1] + opt.window, artist="?", title="?", role="unidentified vocal", lyrics=text
                )
            )
    return out


_HALLUCINATIONS = ("thank you for watching", "thanks for watching", "subscribe", "subtitles by", "♪")


def _whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    name = os.environ.get("TRACKLIST_WHISPER_MODEL", "small")
    return WhisperModel(name, device="cpu", compute_type="int8")


def _transcribe(model, samples: np.ndarray) -> str | None:
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    segments, _ = model.transcribe(
        samples.astype(np.float32),
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.5,
    )
    words = [s.text.strip() for s in segments if s.avg_logprob > -1.0 and s.no_speech_prob < 0.6]
    text = " ".join(w for w in words if w and not any(h in w.lower() for h in _HALLUCINATIONS))
    return text[:200] if len(text) >= 8 else None

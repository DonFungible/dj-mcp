"""MCP server for Serato DJ Pro: library, crates, cues, history, harmonic mix planning and MIDI deck control."""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import app
from .analysis import analyze, analyze_many
from .fmt import brief, expand_home, locale_key, to_json, write_m3u
from .jsnum import js_str, parse_float, parse_iso, to_fixed
from .keys import camelot_string, compatible_keys, key_compatibility, key_name, parse_key
from .library import Library
from .midi import ACTIONS, PORT_NAME, bridge, mapping_table, message_for, perform, sweep, tempo_value
from .models import Session, Track
from .planner import (
    CURVES,
    NULL,
    MixTrack,
    PlanOptions,
    analyze_set,
    bars_to_sec,
    dedupe,
    identity,
    label,
    mix_points,
    mmss,
    plan_mix,
    reorder_set,
    suggest_next,
    transition,
    with_energy,
)
from .sqlite import master_path, sqlite_lock_owner
from .writer import (
    backup_base,
    delete_crate,
    edit_tracks,
    relocate_missing,
    remove_from_crate,
    rename_crate,
    serato_running,
    write_crate_tracks,
)

mcp = MCPServer(
    "serato-mcp",
    instructions=(
        "Tools for Serato DJ Pro: search the library, read cues/beatgrids, manage crates, read play history, plan "
        "harmonic mixes, and control decks over a virtual MIDI device. Writes to the library refuse while Serato is "
        "running (quit it first, or pass force). With Serato DJ 4, crate changes are written to the legacy _Serato_ "
        "files and imported by Serato on its next launch. Deck control needs a one-time MIDI mapping: run "
        "midi_setup, then midi_learn for each action you want."
    ),
)

lib = Library()


def L() -> Library:  # noqa: N802 - short on purpose, used everywhere
    return lib.refresh()


def tool(read_only: bool = True, name: str | None = None) -> Callable[[Callable], Callable]:
    """Register a tool that returns pretty-printed JSON; any failure becomes an error result with its message."""
    annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=False, open_world_hint=False)

    def decorate(fn: Callable) -> Callable:
        params = set(inspect.signature(fn).parameters)

        def py_names(kwargs: dict) -> dict:
            # Parameters named after Python keywords ("from") arrive under their public name.
            return {k + "_" if k not in params and k + "_" in params else k: v for k, v in kwargs.items()}

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs) -> str:
                try:
                    return to_json(await fn(*args, **py_names(kwargs)))
                except Exception as err:  # noqa: BLE001 - report every failure to the model
                    raise ToolError(str(err)) from err

        else:

            @functools.wraps(fn)
            def wrapper(*args, **kwargs) -> str:
                try:
                    return to_json(fn(*args, **py_names(kwargs)))
                except Exception as err:  # noqa: BLE001
                    raise ToolError(str(err)) from err

        wrapper.__annotations__ = {**fn.__annotations__, "return": str}
        return mcp.tool(name=name, annotations=annotations, structured_output=False)(wrapper)

    return decorate


# ---- shared parameters and helpers ---------------------------------------------------------

TrackRefs = Annotated[list[str], Field(description="Tracks as absolute paths or search text like 'artist - title'")]
Force = Annotated[
    bool | None,
    Field(
        description="Write even though Serato is running. Serato may overwrite or corrupt the change; avoid unless you "
        "know Serato is idle"
    ),
]
Deck = Annotated[int, Field(ge=1, le=4)]
ActionName = Literal[tuple(ACTIONS)]  # type: ignore[valid-type]
CurveName = Literal[tuple(CURVES)]  # type: ignore[valid-type]

Crate_ = Annotated[str | None, Field(description="Only use tracks from this crate")]
Genre = Annotated[str | None, Field(description="Only these genres (comma-separated, substring match)")]
IncludeMissing = Annotated[bool | None, Field(description="Include tracks whose files are missing (default false)")]
IncludeStreaming = Annotated[
    bool | None, Field(description="Include streaming tracks (Spotify, Tidal...) from Serato's library (default false)")
]
Exclude = Annotated[list[str] | None, Field(description="Tracks to leave out (paths or search text)")]


def resolve_all(refs: list[str]) -> list[Track]:
    return [L().resolve(r) for r in refs]


def as_mix(tracks: list[Track], pool: list[Track] | None = None) -> list[MixTrack]:
    everything = list(dict.fromkeys([*(tracks if pool is None else pool), *tracks]))
    analyses = analyze_many(everything)
    by_path = {m.track.path: m for m in with_energy(everything, analyses)}
    return [by_path[t.path] for t in tracks]


def pool_tracks(
    crate: str | None = None,
    genre: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    include_missing: bool | None = None,
    include_streaming: bool | None = None,
    exclude: list[str] | None = None,
) -> list[Track]:
    lb = L()
    playable = lb.playable(include_streaming=bool(include_streaming), include_missing=bool(include_missing))
    if crate:
        allowed = set(playable)
        tracks = [t for t in (lb.track(p) for p in lb.crate(crate).trackPaths) if t and t in allowed]
    else:
        tracks = list(playable)
    if genre:
        wanted = [s.strip() for s in genre.lower().split(",")]
        tracks = [t for t in tracks if t.genre and any(x in t.genre.lower() for x in wanted)]
    if bpm_min:
        tracks = [t for t in tracks if t.bpm and t.bpm >= bpm_min]
    if bpm_max:
        tracks = [t for t in tracks if t.bpm and t.bpm <= bpm_max]
    if exclude:
        ex = {identity(t) for t in resolve_all(exclude)}
        tracks = [t for t in tracks if identity(t) not in ex]
    return tracks


def latest_session() -> Session | None:
    sessions = L().sessions()
    return sessions[0] if sessions else None


def crate_tracks(name: str) -> list[Track]:
    lb = L()
    return [t for t in (lb.track(p) for p in lb.crate(name).trackPaths) if t]


def signed(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{js_str(x)}"


def q(x: object) -> str:
    """A value inside a sentence, "?" when unknown."""
    return "?" if x is None else js_str(x)


# ---- library ---------------------------------------------------------------------------------


@tool()
def serato_status() -> Any:
    """Overview: which Serato library is in use, whether Serato DJ is running, track/crate counts, history and
    MIDI status."""
    lb = L()
    sessions = lb.sessions()
    sources: dict[str, int] = {}
    for t in lb.tracks:
        sources[t.source or "local"] = sources.get(t.source or "local", 0) + 1
    running = serato_running()
    last = sessions[0] if sessions else None
    return {
        "seratoRunning": running,
        "libraryLock": sqlite_lock_owner(),
        "writesAllowed": not running and os.environ.get("SERATO_MCP_READONLY") != "1",
        "backend": f"Serato DJ 4+ library ({master_path()})"
        if lb.backend == "sqlite"
        else "Legacy database V2 + crate files",
        "legacyFolders": [{"folder": r.dir, "drive": r.volumeRoot} for r in lb.roots],
        "tracks": len(lb.tracks),
        "bySource": sources,
        "playableLocalTracks": len(lb.playable()),
        "missingFiles": sum(1 for t in lb.tracks if t.missing),
        "crates": sum(1 for c in lb.crates if c.space is None or c.space == "Serato Library"),
        "playlists": sum(1 for c in lb.crates if c.space and c.space != "Serato Library"),
        "pendingImport": [c.name for c in lb.crates if c.pendingImport],
        "historySessions": len(sessions),
        "lastSession": {
            "name": last.name,
            "startedAt": last.startedAt,
            "endedAt": last.endedAt,
            "tracks": len(last.entries),
        }
        if last
        else NULL,
        "midi": {"port": PORT_NAME, "actions": len(ACTIONS), **bridge.ports()},
        "backups": backup_base(),
    }


@tool()
def search_tracks(
    query: Annotated[
        str | None, Field(description="Text matched against artist, title, album, genre, label, remixer and file name")
    ] = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    key: Annotated[str | None, Field(description="Exact key in any notation: 8A, Am, A minor, 1m")] = None,
    compatible_with_key: Annotated[
        str | None, Field(description="Only keys that mix harmonically with this key")
    ] = None,
    energy_min: float | None = None,
    energy_max: float | None = None,
    genre: str | None = None,
    crate: str | None = None,
    include_missing: bool | None = None,
    include_streaming: Annotated[
        bool | None, Field(description="Also search streaming tracks in Serato's library (default false)")
    ] = None,
    sort: Literal["relevance", "bpm", "key", "energy", "added", "artist", "title"] | None = None,
    limit: Annotated[int | None, Field(ge=1, le=500)] = None,
) -> Any:
    """Search and filter the Serato library by text, BPM, key, harmonic compatibility, energy, genre or crate."""
    lb = L()
    pool = lb.playable(include_streaming=bool(include_streaming), include_missing=bool(include_missing))
    if crate:
        in_crate = set(lb.crate(crate).trackPaths)
        pool = [t for t in pool if t.path in in_crate]
    tracks = lb.search(query, 100000, pool) if query else list(pool)
    if bpm_min:
        tracks = [t for t in tracks if t.bpm and t.bpm >= bpm_min]
    if bpm_max:
        tracks = [t for t in tracks if t.bpm and t.bpm <= bpm_max]
    if key:
        k = parse_key(key)
        if not k:
            raise ValueError(f'Unrecognised key "{key}"')
        tracks = [t for t in tracks if t.camelot == camelot_string(k)]
    if compatible_with_key:
        k = parse_key(compatible_with_key)
        if not k:
            raise ValueError(f'Unrecognised key "{compatible_with_key}"')
        ok = {x["key"] for x in compatible_keys(k)}
        tracks = [t for t in tracks if t.camelot and t.camelot in ok]
    if energy_min:
        tracks = [t for t in tracks if t.energy and t.energy >= energy_min]
    if energy_max:
        tracks = [t for t in tracks if t.energy and t.energy <= energy_max]
    if genre:
        tracks = [t for t in tracks if t.genre and genre.lower() in t.genre.lower()]
    sorters: dict[str, tuple[Callable[[Track], Any], bool]] = {
        "bpm": (lambda t: t.bpm or 0, False),
        "key": (lambda t: (int(parse_float(t.camelot or "99")), t.camelot or ""), False),
        "energy": (lambda t: t.energy or 0, True),
        "added": (lambda t: t.addedAt or "", True),
        "artist": (lambda t: locale_key(t.artist or ""), False),
        "title": (lambda t: locale_key(t.title or ""), False),
    }
    if sort and sort != "relevance":
        fn, desc = sorters[sort]
        tracks = sorted(tracks, key=fn, reverse=desc)
    n = limit or 50
    return {"total": len(tracks), "showing": min(n, len(tracks)), "tracks": [brief(t) for t in tracks[:n]]}


@tool()
def get_track(track: Annotated[str, Field(description="Absolute path or search text")]) -> Any:
    """Full details for one track: all Serato metadata plus hot cues, saved loops, beatgrid, track colour and Mixed In
    Key data read from the file, mix-in/out points and compatible keys."""
    t = L().resolve(track)
    an = analyze(t)
    (m,) = as_mix([t])
    k = parse_key(t.camelot or t.key or (an.mikKey if an else None))
    if an:
        file_analysis: Any = {
            "hotCues": [
                {
                    "slot": c.index + 1,
                    "at": mmss(c.positionMs / 1000),
                    "ms": c.positionMs,
                    "name": c.name or None,
                    "color": c.color,
                }
                for c in an.cues
            ],
            "loops": [
                {
                    "slot": lp.index + 1,
                    "from": mmss(lp.startMs / 1000),
                    "to": mmss(lp.endMs / 1000),
                    "name": lp.name or None,
                    "locked": lp.locked,
                }
                for lp in an.loops
            ],
            "beatGrid": an.beatGrid,
            "trackColor": an.trackColor,
            "bpmLocked": an.bpmLocked,
            "seratoAutoBpm": an.autoBpm,
            "autoGainDb": an.autoGainDb,
            "mixedInKey": {"key": an.mikKey, "energy": an.mikEnergy} if an.mikKey or an.mikEnergy else None,
        }
    else:
        file_analysis = "File is missing; use relocate_missing_tracks" if t.missing else "Could not read file tags"
    p = mix_points(m)
    return {
        **{f: getattr(t, f) for f in t.__dataclass_fields__},
        "fileAnalysis": file_analysis,
        "energy": m.energy,
        "energyEstimated": m.energyEstimated,
        "mixPoints": {"playFrom": mmss(p["mixInSec"]), "mixOutAt": mmss(p["mixOutSec"]), "basedOn": p["source"]},
        "phrase": {f"{b} bars": f"{to_fixed(bars_to_sec(b, t.bpm), 1)}s" for b in (8, 16, 32)} if t.bpm else None,
        "compatibleKeys": [f"{x['key']} ({key_name(parse_key(x['key']))}, {x['relation']})" for x in compatible_keys(k)]  # type: ignore[arg-type]
        if k
        else None,
    }


@tool()
def library_stats(crate: str | None = None) -> Any:
    """Library breakdown by genre, BPM, key and energy, plus health checks (missing files, unanalysed tracks,
    duplicates)."""
    lb = L()
    tracks = crate_tracks(crate) if crate else lb.tracks

    def count(f: Callable[[Track], str | None]) -> dict[str, int]:
        m: dict[str, int] = {}
        for t in tracks:
            k = f(t) or "(none)"
            m[k] = m.get(k, 0) + 1
        return dict(sorted(m.items(), key=lambda kv: -kv[1]))

    ids: dict[str, int] = {}
    for t in tracks:
        ids[identity(t)] = ids.get(identity(t), 0) + 1

    def bpm_bucket(t: Track) -> str | None:
        if not t.bpm:
            return None
        lo = int(t.bpm // 5) * 5
        return f"{lo}-{lo + 4}"

    return {
        "tracks": len(tracks),
        "genres": count(lambda t: t.genre),
        "bpm": count(bpm_bucket),
        "keys": count(lambda t: t.camelot),
        "energy": count(lambda t: str(t.energy) if t.energy else None),
        "health": {
            "missingFiles": sum(1 for t in tracks if t.missing),
            "noBpm": sum(1 for t in tracks if not t.bpm),
            "noKey": sum(1 for t in tracks if not t.camelot),
            "corrupt": sum(1 for t in tracks if t.corrupt),
            "duplicateGroups": sum(1 for n in ids.values() if n > 1),
        },
    }


@tool()
def find_duplicates() -> Any:
    """List tracks that appear more than once in the library (same artist and title, different files)."""
    groups: dict[str, list[Track]] = {}
    for t in L().tracks:
        groups.setdefault(identity(t), []).append(t)
    return [
        {
            "track": label(g[0]),
            "copies": [{"path": t.path, "bitrate": t.bitrate, "missing": t.missing, "crates": t.crates} for t in g],
        }
        for g in groups.values()
        if len(g) > 1 and g[0].artist and g[0].title
    ]


# ---- crates ------------------------------------------------------------------------------------


@tool()
def list_crates(
    include_playlists: Annotated[
        bool | None, Field(description="Also list iTunes and streaming playlists (default true)")
    ] = None,
) -> Any:
    """List crates, smart crates and (optionally) iTunes / streaming playlists with track counts. Nested crates are
    shown as 'Parent / Child'."""
    keep_playlists = True if include_playlists is None else include_playlists
    out = []
    for c in L().crates:
        if not (keep_playlists or not c.space or c.space == "Serato Library"):
            continue
        if c.kind == "streaming folder" and not c.trackPaths:
            continue
        item: dict[str, Any] = {"name": c.name, "kind": c.kind or "crate", "tracks": len(c.trackPaths)}
        if c.pendingImport:
            item["pendingImport"] = True
        if c.rules:
            item["rules"] = c.rules
        out.append(item)
    return out


@tool()
def get_crate(name: str, limit: int | None = None) -> Any:
    """Tracks in a crate, in crate order."""
    lb = L()
    c = lb.crate(name)
    tracks = [lb.track(p) or {"path": p, "notInDatabase": True} for p in c.trackPaths]
    out: dict[str, Any] = {"name": c.name, "kind": c.kind or "crate"}
    if c.pendingImport:
        out["pendingImport"] = True
    if c.rules:
        out["rules"] = c.rules
    out["count"] = len(tracks)
    shown = tracks[: limit if limit is not None else 500]
    out["tracks"] = [t if isinstance(t, dict) else brief(t) for t in shown]
    return out


@tool(read_only=False)
def create_crate(
    name: str,
    tracks: TrackRefs,
    mode: Annotated[
        Literal["create", "replace", "append"] | None, Field(description="Default create (fails if it exists)")
    ] = None,
    force: Force = None,
) -> Any:
    """Create a crate (use 'Parent / Child' for a sub-crate), or replace/append its tracks. Serato must be closed."""
    return write_crate_tracks(L(), name, [t.path for t in resolve_all(tracks)], mode or "create", force)


@tool(read_only=False)
def add_to_crate(name: str, tracks: TrackRefs, force: Force = None) -> Any:
    """Add tracks to a crate (creates it if needed). Skips tracks already in it. Serato must be closed."""
    return write_crate_tracks(L(), name, [t.path for t in resolve_all(tracks)], "append", force)


@tool(read_only=False, name="remove_from_crate")
def remove_from_crate_(name: str, tracks: TrackRefs, force: Force = None) -> Any:
    """Remove tracks from a crate (the files and library entries stay). Serato must be closed."""
    return remove_from_crate(L(), name, [t.path for t in resolve_all(tracks)], force)


@tool(read_only=False, name="rename_crate")
def rename_crate_(name: str, new_name: str, force: Force = None) -> Any:
    """Rename or move a crate (sub-crates move with it). Serato must be closed."""
    return rename_crate(L(), name, new_name, force)


@tool(read_only=False, name="delete_crate")
def delete_crate_(name: str, force: Force = None) -> Any:
    """Delete a crate and its sub-crates. Tracks stay in the library; a backup copy is kept. Serato must be closed."""
    return delete_crate(L(), name, force)


# ---- track metadata ----------------------------------------------------------------------------


class TrackEdit(BaseModel):
    track: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    comment: str | None = None
    grouping: str | None = None
    remixer: str | None = None
    composer: str | None = None
    label: str | None = None
    year: str | None = None
    key: str | None = None
    bpm: str | None = None
    color: str | None = Field(default=None, description="Track colour as #rrggbb (#ffffff = none)")


@tool(read_only=False)
def update_tracks(edits: Annotated[list[TrackEdit], Field(min_length=1)], force: Force = None) -> Any:
    """Edit Serato library metadata (title, artist, genre, comment, key, BPM, colour...) for one or more tracks.
    Changes go into Serato's database, not the audio file tags. Serato must be closed."""
    changes = []
    for e in edits:
        values = e.model_dump(exclude={"track"}, exclude_none=True)
        changes.append((L().resolve(e.track).path, values))
    return edit_tracks(L(), changes, force)


@tool(read_only=False)
def relocate_missing_tracks(
    search_dirs: Annotated[list[str] | None, Field(description="Folders to search (default: ~/Music)")] = None,
    dry_run: Annotated[bool | None, Field(description="Default true. Set false to apply")] = None,
    force: Force = None,
) -> Any:
    """Find files Serato can't locate (moved or renamed) by searching folders, then repoint the library and every
    crate at them. Defaults to a dry run that only reports matches."""
    dirs = [expand_home(d) for d in search_dirs] if search_dirs else [str(Path.home() / "Music")]
    return relocate_missing(L(), dirs, True if dry_run is None else dry_run, force)


# ---- history ------------------------------------------------------------------------------------


@tool()
def list_sessions(limit: int | None = None) -> Any:
    """Recent Serato play-history sessions (newest first)."""
    sessions = L().sessions()[: limit if limit is not None else 20]
    return [
        {"id": s.id, "name": s.name, "startedAt": s.startedAt, "endedAt": s.endedAt, "tracks": len(s.entries)}
        for s in sessions
    ]


@tool()
def get_session(session: Annotated[str | None, Field(description="Session id or name")] = None) -> Any:
    """Tracks played in a history session, in order (default: the latest)."""
    sessions = L().sessions()
    if session:
        s = next(
            (x for x in sessions if str(x.id) == session or x.name == session or x.name.startswith(session + " ")), None
        )
    else:
        s = sessions[0] if sessions else None
    if not s:
        raise ValueError(
            f'No session "{session}". Use list_sessions.'
            if sessions
            else "No Serato history yet. It appears once tracks are played in Serato DJ."
        )
    return {
        "id": s.id,
        "name": s.name,
        "startedAt": s.startedAt,
        "endedAt": s.endedAt,
        "tracks": [
            {
                "#": i + 1,
                "at": e.startTime,
                "deck": e.deck,
                "artist": e.artist,
                "title": e.title,
                "bpm": e.bpm,
                "key": e.key,
                "playedFor": mmss(e.playTimeSec) if e.playTimeSec is not None and e.playTimeSec < 7200 else None,
                "path": e.path,
            }
            for i, e in enumerate(s.entries)
        ],
    }


@tool()
def now_playing() -> Any:
    """What is (or was last) loaded on each deck, from Serato's play history."""
    s = latest_session()
    if not s:
        return {"playing": NULL, "note": "No Serato history yet. It appears after tracks are played in Serato DJ."}
    running = serato_running()
    live = running and (not s.endedAt or time.time() * 1000 - parse_iso(s.modifiedAt) < 30 * 60 * 1000)
    decks: dict[str, Any] = {}
    for e in s.entries:
        if e.deck:
            decks[f"deck {e.deck}"] = {
                "artist": e.artist, "title": e.title, "bpm": e.bpm, "key": e.key,
                "loadedAt": e.startTime, "path": e.path,
            }  # fmt: skip
    last = s.entries[-1] if s.entries else None
    last_loaded = f"{last.artist or ''} - {last.title or ''} (deck {last.deck or '?'})" if last else NULL
    return {"session": s.name, "live": live, "seratoRunning": running, "decks": decks, "lastLoaded": last_loaded}


@tool()
def play_stats(limit: int | None = None) -> Any:
    """Your DJ habits from Serato: most-played tracks, recently played, and the transitions you play most often
    (track A followed by track B on another deck)."""
    lb = L()
    n = limit if limit is not None else 15
    top = sorted((t for t in lb.tracks if (t.playCount or 0) > 0), key=lambda t: -(t.playCount or 0))[:n]
    pairs: dict[str, dict[str, Any]] = {}
    plays: dict[str, int] = {}
    for s in lb.sessions():
        es = [e for e in s.entries if e.title]
        for e in es:
            k = f"{e.artist or ''} - {e.title}"
            plays[k] = plays.get(k, 0) + 1
        for i in range(1, len(es)):
            if es[i].deck and es[i - 1].deck and es[i].deck == es[i - 1].deck:
                continue  # same deck = not a mix
            src = f"{es[i - 1].artist or ''} - {es[i - 1].title}"
            dst = f"{es[i].artist or ''} - {es[i].title}"
            if src == dst:
                continue
            p = pairs.setdefault(src + "→" + dst, {"from": src, "to": dst, "times": 0, "sessions": set()})
            p["times"] += 1
            p["sessions"].add(s.name)
    return {
        "mostPlayed": [{"plays": t.playCount, **brief(t)} for t in top],
        "mostPlayedInHistory": [{"track": k, "times": v} for k, v in sorted(plays.items(), key=lambda kv: -kv[1])[:n]],
        "favouriteTransitions": [
            {"from": p["from"], "to": p["to"], "times": p["times"], "sessions": len(p["sessions"])}
            for p in sorted(pairs.values(), key=lambda p: -p["times"])[:n]
        ],
    }


# ---- mixing ---------------------------------------------------------------------------------------


@tool()
def analyze_transition(from_: Annotated[str, Field(alias="from")], to: str) -> Any:
    """How well one track mixes into another: key relation, tempo change, energy change, recommended technique,
    mix-out/in points and warnings."""
    x, y = as_mix(resolve_all([from_, to]))
    return transition(x, y)


@tool()
def suggest_next_tracks(
    track: Annotated[
        str | None, Field(description="Current track. Default: the last track in the live history")
    ] = None,
    energy: Annotated[
        Literal["up", "down", "same", "any"] | None, Field(description="Where to take the energy (default any)")
    ] = None,
    limit: int | None = None,
    exclude_played: Annotated[
        bool | None, Field(description="Skip tracks already played in the latest session (default true)")
    ] = None,
    crate: Crate_ = None,
    genre: Genre = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    include_missing: IncludeMissing = None,
    include_streaming: IncludeStreaming = None,
    exclude: Exclude = None,
) -> Any:
    """Best tracks to play after a given track (or after what's on the deck now), ranked by harmonic, tempo and
    energy fit."""
    ref = track
    s = latest_session()
    if not ref:
        last = s.entries[-1] if s and s.entries else None
        if not last or (not last.path and not last.title):
            raise ValueError("No track given and no history to work out what's playing")
        ref = last.path or f"{last.artist or ''} {last.title or ''}"
    current = L().resolve(ref)
    skip: set[str] = set()
    if exclude_played is None or exclude_played:
        for e in s.entries if s else []:
            if e.path:
                skip.add(e.path)
                played = L().track(e.path)
                if played:
                    skip.add(identity(played))
    pool = pool_tracks(crate, genre, bpm_min, bpm_max, include_missing, include_streaming, exclude)
    mix = as_mix(pool, pool)
    (cur,) = as_mix([current], pool)
    suggestions = []
    for x in suggest_next(cur, mix, limit or 10, energy, skip):
        b = brief(x["track"])
        tr = x["transition"]
        suggestions.append(
            {
                "score": x["score"],
                **b,
                "key": b["key"] if b["key"] is not None else tr["key"]["to"],
                "how": f"{tr['key']['relation']} key ({q(tr['key']['from'])}→{q(tr['key']['to'])}), "
                f"{signed(tr['tempo']['pitchChangePct'])}% tempo, "
                f"energy {q(tr['energy']['from'])}→{q(tr['energy']['to'])}",
                "technique": tr["technique"],
            }
        )
    return {"current": brief(current), "suggestions": suggestions}


@tool(read_only=False, name="plan_mix")
def plan_mix_(
    duration_min: Annotated[float | None, Field(description="Target set length in minutes")] = None,
    count: Annotated[int | None, Field(ge=2, le=200, description="Number of tracks (overrides duration_min)")] = None,
    curve: Annotated[
        CurveName | None, Field(description="; ".join(f"{k}: {v.description}" for k, v in CURVES.items()))
    ] = None,
    custom_curve: Annotated[
        list[Annotated[float, Field(ge=1, le=10)]] | None,
        Field(description="Energy targets (1-10) spread across the set, e.g. [4,6,8,9,7]"),
    ] = None,
    start_bpm: float | None = None,
    end_bpm: float | None = None,
    start_with: Annotated[str | None, Field(description="Opening track")] = None,
    end_with: Annotated[str | None, Field(description="Closing track")] = None,
    must_include: list[str] | None = None,
    harmonic: Annotated[
        Literal["strict", "balanced", "loose"] | None,
        Field(description="How much key compatibility matters (default balanced)"),
    ] = None,
    max_bpm_jump_pct: Annotated[
        float | None, Field(description="Largest tempo change between tracks (default 8%)")
    ] = None,
    exclude_played: Annotated[
        bool | None, Field(description="Leave out tracks played in the latest history session")
    ] = None,
    save_as_crate: Annotated[
        str | None, Field(description="Also save the set as this crate (Serato must be closed)")
    ] = None,
    export_m3u: Annotated[str | None, Field(description="Also write an .m3u8 playlist to this path")] = None,
    force: Force = None,
    crate: Crate_ = None,
    genre: Genre = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    include_missing: IncludeMissing = None,
    include_streaming: IncludeStreaming = None,
    exclude: Exclude = None,
) -> Any:
    """Build a complete DJ set from the library: picks and orders tracks for harmonic flow, smooth tempo and a target
    energy curve, with timing, mix points and a technique for every transition. Optionally saves it as a crate
    and/or M3U playlist."""
    skip = list(exclude or [])
    if exclude_played:
        s = latest_session()
        skip += [e.path for e in (s.entries if s else []) if e.path and L().track(e.path)]
    pool = pool_tracks(crate, genre, bpm_min, bpm_max, include_missing, include_streaming, skip)
    pin_refs = [r for r in (start_with, end_with) if r] + list(must_include or [])
    pins = resolve_all(pin_refs)
    mix = as_mix(list(dict.fromkeys([*pool, *pins])))

    def find(t: Track) -> MixTrack:
        return next(m for m in mix if m.track.path == t.path)

    i = 0
    start = end = None
    if start_with:
        start = find(pins[i])
        i += 1
    if end_with:
        end = find(pins[i])
        i += 1
    plan = plan_mix(
        mix,
        PlanOptions(
            count=count,
            durationMin=duration_min,
            curve=curve,
            customCurve=custom_curve,
            startBpm=start_bpm,
            endBpm=end_bpm,
            start=start,
            end=end,
            mustInclude=[find(t) for t in pins[i:]],
            harmonic=harmonic,
            maxBpmJumpPct=max_bpm_jump_pct,
        ),
    )
    setlist = []
    for idx, p in enumerate(plan["tracks"]):
        tr = plan["transitions"][idx] if idx < len(plan["transitions"]) else None
        energy = None
        if p["energy"] is not None:
            energy = (
                f"{js_str(p['energy'])}{' (est.)' if p['energyEstimated'] else ''} / target {js_str(p['targetEnergy'])}"
            )
        setlist.append(
            {
                "#": p["position"],
                "at": p["startsAt"],
                **brief(p["track"]),
                "energy": energy,
                "playFrom": p["playFrom"],
                "mixOutAt": p["mixOutAt"],
                "nextTransition": f"{tr['score']}/100 · {tr['key']['relation']} key · "
                f"{signed(tr['tempo']['pitchChangePct'])}% · "
                f"{tr['technique']}"
                if tr
                else None,
            }
        )
    out: dict[str, Any] = {
        "summary": plan["summary"],
        "warnings": plan["warnings"],
        "setlist": setlist,
        "transitions": plan["transitions"],
    }
    if save_as_crate:
        out["crate"] = write_crate_tracks(
            L(), save_as_crate, [p["track"].path for p in plan["tracks"]], "replace", force
        )
    if export_m3u:
        out["playlist"] = write_m3u(expand_home(export_m3u), [p["track"] for p in plan["tracks"]])
    return out


@tool(name="analyze_set")
def analyze_set_(
    tracks: Annotated[
        list[str] | None, Field(description="Tracks as absolute paths or search text like 'artist - title'")
    ] = None,
    crate: Annotated[str | None, Field(description="Use a crate's order as the setlist")] = None,
    reorder: Annotated[bool | None, Field(description="Also suggest an improved order")] = None,
    keep_first: bool | None = None,
    keep_last: bool | None = None,
) -> Any:
    """Score a setlist you already have, transition by transition, and optionally propose a better order of the same
    tracks."""
    chosen = resolve_all(tracks) if tracks else crate_tracks(crate) if crate else []
    if len(chosen) < 2:
        raise ValueError("Give at least 2 tracks or a crate")
    mix = as_mix(chosen)
    current = analyze_set(mix)
    out: dict[str, Any] = {
        "current": {
            "averageScore": current["averageTransitionScore"],
            "transitions": [
                {
                    "#": f"{i + 1}→{i + 2}",
                    "score": t["score"],
                    "from": t["from"],
                    "to": t["to"],
                    "key": f"{q(t['key']['from'])}→{q(t['key']['to'])} ({t['key']['relation']})",
                    "tempo": f"{js_str(t['tempo']['pitchChangePct'])}%",
                    "technique": t["technique"],
                }
                for i, t in enumerate(current["transitions"])
            ],
        }
    }
    if reorder:
        better = reorder_set(dedupe(mix), bool(keep_first), bool(keep_last))
        out["suggestedOrder"] = {
            "averageScore": better["summary"]["averageTransitionScore"],
            "keyPath": better["summary"]["keyPath"],
            "tracks": [label(p["track"]) for p in better["tracks"]],
            "paths": [p["track"].path for p in better["tracks"]],
        }
    return out


@tool(name="compatible_keys")
def compatible_keys_(key: str) -> Any:
    """Keys that mix harmonically with a key (any notation), using the Camelot wheel."""
    k = parse_key(key)
    if not k:
        raise ValueError(f'Unrecognised key "{key}"')
    return {
        "key": f"{camelot_string(k)} ({key_name(k)})",
        "compatible": [
            {**x, "name": key_name(parse_key(x["key"])), "why": key_compatibility(k, parse_key(x["key"])).description}  # type: ignore[arg-type]
            for x in compatible_keys(k)
        ],
    }


@tool(read_only=False)
def export_playlist(
    path: Annotated[str, Field(description="Output file, e.g. ~/Desktop/set.m3u8")],
    tracks: Annotated[
        list[str] | None, Field(description="Tracks as absolute paths or search text like 'artist - title'")
    ] = None,
    crate: str | None = None,
) -> Any:
    """Write tracks (or a crate) to an .m3u8 playlist file, e.g. to share a set or import it elsewhere."""
    chosen = resolve_all(tracks) if tracks else crate_tracks(crate) if crate else []
    if not chosen:
        raise ValueError("Give tracks or a crate")
    return {"file": write_m3u(expand_home(path), chosen), "tracks": len(chosen)}


# ---- MIDI deck control ----------------------------------------------------------------------------


@tool(read_only=False)
def midi_setup() -> Any:
    """Open the 'Serato MCP' virtual MIDI device and get the one-time steps to map it in Serato DJ Pro."""
    port = bridge.open()
    return {
        **port,
        "steps": [
            f'In Serato DJ Pro open Setup > MIDI and make sure "{PORT_NAME}" is listed and enabled '
            "(restart Serato if it "
            "isn't there yet; this MCP server must be running).",
            "Click the MIDI button at the top of Serato to enter MIDI assign mode.",
            "Click the on-screen control you want to map (e.g. deck 1 play), then call midi_learn with that action and "
            "deck. Serato binds the message it receives.",
            "Repeat for each control you want, then leave assign mode and save the preset in Setup > MIDI.",
            "From then on use deck_control, set_tempo, fader_sweep and auto_transition.",
        ],
        "tip": "Mapping order that covers most needs: play_pause, cue, sync, load_selected, volume, eq_low, filter, "
        "tempo "
        "for decks 1 and 2, then crossfader.",
        "ports": bridge.ports(),
    }


@tool()
def midi_mapping() -> Any:
    """The full table of MIDI messages serato-mcp sends for each action."""
    return mapping_table()


@tool(read_only=False)
async def midi_learn(
    action: ActionName,
    deck: Deck | None = None,
    repeat: Annotated[int | None, Field(ge=1, le=20)] = None,
    interval_ms: Annotated[int | None, Field(ge=100, le=5000)] = None,
) -> Any:
    """Send one action's MIDI message (optionally repeated) so Serato can learn it in MIDI assign mode. Click the
    target control in Serato first."""
    bridge.open()
    msg = None
    times = repeat or 1
    for i in range(times):
        if ACTIONS[action].kind == "fader":
            await sweep(action, deck, 0, 127, 300)
            msg = message_for(action, deck, 127)
        else:
            msg = await perform(action, deck)
        if i < times - 1:
            await asyncio.sleep((interval_ms or 1000) / 1000)
    return {"sent": msg, "mapTo": ACTIONS[action].serato}


@tool(read_only=False)
async def deck_control(
    action: ActionName,
    deck: Annotated[
        int | None,
        Field(ge=1, le=4, description="1-4 for deck actions; omit for global ones (crossfader, library, sampler)"),
    ] = None,
    value: Annotated[float | None, Field(ge=0, le=127, description="Fader value 0-127 (64 = centre)")] = None,
    hold_ms: Annotated[int | None, Field(description="How long to hold a button")] = None,
) -> Any:
    """Press a deck/mixer control or set a fader through MIDI: play_pause, cue, sync, hotcue_1..8, loops, key lock,
    EQ, filter, volume, crossfader, library browsing, sampler and more (see midi_mapping). Requires the action to be
    mapped in Serato."""
    return {"sent": await perform(action, deck, value, hold_ms)}


class SequenceStep(BaseModel):
    action: str | None = Field(default=None, description="Action name, or omit for a pure wait")
    deck: Annotated[int | None, Field(ge=1, le=4)] = None
    value: Annotated[float | None, Field(ge=0, le=127)] = None
    to_value: Annotated[
        float | None, Field(ge=0, le=127, description="Sweep a fader from value to to_value over duration_ms")
    ] = None
    duration_ms: int | None = None
    wait_ms: Annotated[int | None, Field(description="Pause after this step")] = None


@tool(read_only=False)
async def deck_sequence(steps: Annotated[list[SequenceStep], Field(min_length=1, max_length=100)]) -> Any:
    """Run several deck actions in order with waits between them, e.g. load, sync, play, then a filter sweep. Each
    step is an action with optional deck/value, or a wait."""
    done: list[str] = []
    for s in steps:
        if s.action:
            if s.action not in ACTIONS:
                raise ValueError(f"Unknown action {s.action}")
            on = f" deck {s.deck}" if s.deck else ""
            if s.to_value is not None:
                start = s.value if s.value is not None else 64
                await sweep(s.action, s.deck, start, s.to_value, s.duration_ms or 1000)
                done.append(f"{s.action}{on} {js_str(start)}→{js_str(s.to_value)}")
            else:
                await perform(s.action, s.deck, s.value)
                done.append(f"{s.action}{on}{f' = {js_str(s.value)}' if s.value is not None else ''}")
        if s.wait_ms:
            await asyncio.sleep(min(s.wait_ms, 120000) / 1000)
    return {"done": done}


@tool(read_only=False)
async def set_tempo(deck: Deck, pitch_pct: float, pitch_range: float | None = None) -> Any:
    """Set a deck's tempo fader to a pitch percentage (needs 'tempo' mapped). Match pitch_range to the deck's range in
    Serato (8, 16 or 50)."""
    rng = pitch_range or 8
    v = tempo_value(pitch_pct, rng)
    await perform("tempo", deck, v)
    return {
        "deck": deck,
        "pitchPct": pitch_pct,
        "faderValue": v,
        "resolution": f"{to_fixed(2 * rng / 127, 2)}% per step",
    }


@tool(read_only=False)
async def fader_sweep(
    action: ActionName,
    from_: Annotated[float, Field(alias="from", ge=0, le=127)],
    to: Annotated[float, Field(ge=0, le=127)],
    duration_ms: Annotated[int, Field(ge=50, le=300000)],
    deck: Deck | None = None,
) -> Any:
    """Move a fader smoothly over time: a crossfade, filter sweep, volume fade or EQ swap."""
    return {"steps": await sweep(action, deck, from_, to, duration_ms)}


@tool(read_only=False)
async def auto_transition(
    from_deck: Deck,
    to_deck: Deck,
    bars: Annotated[int | None, Field(ge=1, le=64, description="Length of the blend (default 16)")] = None,
    bpm: Annotated[
        float | None,
        Field(description="Tempo to time the bars (default: the incoming track's BPM from history, else 126)"),
    ] = None,
    start_incoming: Annotated[
        bool | None, Field(description="Press sync + play on the incoming deck first (default true)")
    ] = None,
    stop_outgoing: Annotated[
        bool | None, Field(description="Pause the outgoing deck at the end (default true)")
    ] = None,
) -> Any:
    """Perform a full blend from one deck to another over N bars: syncs and starts the incoming deck, fades it in,
    swaps the bass halfway, then fades the outgoing deck out. Needs play_pause, sync, volume and eq_low mapped for
    both decks. The tracks must already be loaded and cued."""
    if from_deck == to_deck:
        raise ValueError("from_deck and to_deck must differ")
    n_bars = bars or 16
    tempo = bpm
    if not tempo:
        s = latest_session()
        on_deck = [x for x in (s.entries if s else []) if x.deck == to_deck]
        tempo = parse_float(on_deck[-1].bpm) if on_deck and on_deck[-1].bpm else 126
    total = bars_to_sec(n_bars, tempo) * 1000
    steps: list[str] = []
    # Prepare incoming: silent, bass cut.
    await perform("volume", to_deck, 0)
    await perform("eq_low", to_deck, 0)
    if start_incoming is None or start_incoming:
        await perform("sync", to_deck)
        await perform("play_pause", to_deck)
        steps.append(f"deck {to_deck}: sync + play")
    await sweep("volume", to_deck, 0, 127, total * 0.4)
    steps.append(f"deck {to_deck}: volume up over {to_fixed(total * 0.4 / 1000, 1)}s")
    # Bass swap on the halfway phrase.
    swap = min(2000, total * 0.1)
    await asyncio.gather(sweep("eq_low", from_deck, 64, 0, swap), sweep("eq_low", to_deck, 0, 64, swap))
    steps.append("bass swap")
    await asyncio.sleep(total * 0.1 / 1000)
    await sweep("volume", from_deck, 127, 0, total * 0.4)
    steps.append(f"deck {from_deck}: volume down")
    if stop_outgoing is None or stop_outgoing:
        await perform("play_pause", from_deck)
        await perform("eq_low", from_deck, 64)
        steps.append(f"deck {from_deck}: paused, EQ reset")
    return {"bars": n_bars, "bpm": tempo, "seconds": float(to_fixed(total / 1000, 1)), "steps": steps}


@tool()
def midi_monitor(limit: int | None = None) -> Any:
    """MIDI messages Serato has sent back to the 'Serato MCP' device (LED feedback for mapped controls), newest last."""
    bridge.open()
    return {"received": bridge.received(limit or 50)}


# ---- app control ---------------------------------------------------------------------------------------


@tool(read_only=False)
async def launch_serato() -> Any:
    """Open Serato DJ Pro."""
    return await app.launch()


@tool(read_only=False)
async def quit_serato() -> Any:
    """Quit Serato DJ Pro cleanly (it saves its library). Needed before crate or metadata edits."""
    return await app.quit_app()


@tool(read_only=False)
async def load_track(
    track: str,
    deck: Deck,
    method: Annotated[
        Literal["keyboard", "midi"] | None, Field(description="Default keyboard for decks 1-2, midi for 3-4")
    ] = None,
) -> Any:
    """Load a library track onto a deck: types it into Serato's search box, then loads the top result with
    Shift+Left/Right (decks 1-2) or the MIDI load_selected action (any deck). Needs macOS Accessibility permission."""
    t = L().resolve(track)
    if t.missing:
        raise ValueError(
            f'The file for "{label(t)}" is missing, so Serato can\'t load it. Run relocate_missing_tracks.'
        )
    query = " ".join(x for x in (t.artist, t.title) if x) or os.path.splitext(os.path.basename(t.path))[0]
    await app.search(query)
    how = method or ("keyboard" if deck <= 2 else "midi")
    if how == "keyboard":
        if deck > 2:
            raise ValueError("Keyboard loading only reaches decks 1 and 2; use method midi")
        await app.load_selected(deck)
    else:
        await perform("load_selected", deck)
    return {
        "loaded": label(t),
        "deck": deck,
        "searchedFor": query,
        "method": how,
        "note": "If another track loaded, the search had several matches; refine with search_tracks and try a more "
        "specific track.",
    }


@tool(read_only=False)
async def serato_search_box(query: str) -> Any:
    """Type text into Serato's on-screen library search (Cmd+F)."""
    await app.search(query)
    return {"searched": query}


@tool(read_only=False)
async def send_keystroke(
    key: str, modifiers: list[Literal["command", "shift", "option", "control"]] | None = None
) -> Any:
    """Send a keyboard shortcut to Serato DJ Pro, e.g. key 'left' with ['shift'] to load deck 1. Keys: letters,
    digits, left/right/up/down, return, tab, escape, space, delete."""
    await app.keystroke(key, list(modifiers or []))
    return {"sent": "+".join([*(modifiers or []), key])}


# ---- prompts -------------------------------------------------------------------------------------------


@mcp.prompt(name="plan_set", description="Plan a DJ set from your Serato library")
def plan_set(length_minutes: str | None = None, vibe: str | None = None, crate: str | None = None) -> str:
    return (
        f"Plan a {length_minutes or '60'}-minute DJ set"
        + (f' from my "{crate}" crate' if crate else "")
        + (f" with this vibe: {vibe}" if vibe else "")
        + ". Check serato_status and library_stats first, pick a fitting energy curve, call plan_mix, then walk me "
        "through the set: the arc, the standout transitions, and any risky ones with how to handle them. Offer to "
        "save it as a crate."
    )


@mcp.prompt(name="whats_next", description="Suggest what to play next based on what's on the deck")
def whats_next() -> str:
    return (
        "Use now_playing and suggest_next_tracks to tell me the best 5 tracks to play next, with how to mix into "
        "each one."
    )


# ---- start ---------------------------------------------------------------------------------------------


def main() -> None:
    # Keep the virtual MIDI device present while the server runs so Serato's saved mapping applies.
    if os.environ.get("SERATO_MCP_MIDI") != "off":
        try:
            bridge.open()
        except Exception as err:  # noqa: BLE001 - deck control is optional
            print(f"serato-mcp: MIDI unavailable: {err}", file=sys.stderr)
    roots = ", ".join(r.dir for r in lib.roots) or "no Serato library found"
    print(f"serato-mcp running ({roots})", file=sys.stderr)
    try:
        mcp.run()
    finally:
        bridge.close()


if __name__ == "__main__":
    main()

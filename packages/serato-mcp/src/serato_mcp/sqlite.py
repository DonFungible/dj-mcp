"""Serato DJ 4+ library: ~/Library/Application Support/Serato/Library/master.sqlite.

master.sqlite aggregates one database per location (root.sqlite for the boot drive, one per external
drive, spotify.sqlite for streaming). It is opened read-only; Serato uses WAL mode so reading while it
runs is safe.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .jsnum import iso, js_round, parse_iso
from .keys import camelot_string, key_name, parse_key
from .models import Crate, HistoryEntry, Session, Track


def sqlite_dir() -> str:
    return os.environ.get("SERATO_SQLITE_DIR") or str(
        Path.home() / "Library" / "Application Support" / "Serato" / "Library"
    )


def master_path() -> str:
    return os.path.join(sqlite_dir(), "master.sqlite")


def has_sqlite_library() -> bool:
    return os.environ.get("SERATO_BACKEND") != "legacy" and os.path.exists(master_path())


def clean_name(n: str) -> str:
    """Serato appends U+105E8A + an id to some container names, e.g. "DonJenk\\U00105E8A1"."""
    return re.sub("\U00105e8a.*$", "", n, flags=re.S)


CONTAINER_KIND = {
    1: "crate", 2: "smart crate", 3: "playlist", 4: "playlist folder", 16: "streaming folder",
    17: "streaming playlist", 18: "streaming playlist", 23: "streaming playlist",
}  # fmt: skip

STREAMING_URI = re.compile(r"^[a-z]+://")


def _open() -> sqlite3.Connection:
    db = sqlite3.connect(Path(master_path()).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


@dataclass
class LocationInfo:
    id: int
    streaming: bool
    root: str | None = None  # directory portable_ids are relative to


def _locations(db: sqlite3.Connection) -> dict[int, LocationInfo]:
    out: dict[int, LocationInfo] = {}
    rows = db.execute(
        "SELECT l.id AS id, c.database_uri AS uri FROM location l LEFT JOIN connection c ON c.location_id = l.id"
    )
    for r in rows:
        uri = r["uri"] or ""
        if re.search(r"spotify|tidal|soundcloud|beatport|beatsource|streaming", uri, re.I):
            out[r["id"]] = LocationInfo(r["id"], True)
        elif uri.endswith("/root.sqlite"):
            out[r["id"]] = LocationInfo(r["id"], False, "/")
        else:
            # External drive: its database lives on the drive at <mount>/_Serato_/Library/location.sqlite.
            m = re.match(r"^(/Volumes/[^/]+)/", uri)
            out[r["id"]] = LocationInfo(r["id"], False, m.group(1) + "/" if m else None)
    return out


_volumes: list[str] | None = None


def _guess_volume_root(portable: str) -> str | None:
    global _volumes
    if _volumes is None:
        _volumes = [f"/Volumes/{v}/" for v in os.listdir("/Volumes")] if os.path.exists("/Volumes") else []
    return next((v for v in _volumes if os.path.exists(v + portable)), None)


def split_artist_title(artist: str | None, title: str | None) -> tuple[str | None, str | None]:
    """Untagged files often carry "Artist - Title" (sometimes "07. Artist - Title") in the title."""
    t = (title or "").strip()
    if artist and artist.strip():
        return artist.strip(), t or None
    m = re.match(r"^(?:\d{1,3}[.\s-]+)?(.+?)\s+-\s+(.+)$", t, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, t or None


def parse_energy(*sources: str | None) -> int | None:
    for s in sources:
        m = re.search(r"energy\s*[:\-]?\s*(\d{1,2})", s, re.I) if s else None
        if m and 1 <= int(m.group(1)) <= 10:
            return int(m.group(1))
    return None


def _comment_key(comment: str | None):
    m = re.match(r"^\s*(\d{1,2}[AB])\b", comment or "", re.I)
    return parse_key(m.group(1)) if m else None


def _safe_json(s: str):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return s


def load_sqlite() -> tuple[list[Track], list[Crate]]:
    with closing(_open()) as db:
        locs = _locations(db)
        assets = db.execute(
            """SELECT id, location_id, portable_id, file_name, file_size, file_bit_rate, file_sample_rate, format,
                      artist,
                      color, comments, grouping, remixer, name, album, composer, year, genre, key, label, rating, bpm,
                      length_ms, time_added, is_corrupt, is_missing, is_beatgrid_locked, third_party_type,
                      dj_play_count, dj_recently_played
               FROM asset WHERE type = 'audio' OR type = ''"""
        ).fetchall()
        tracks: list[Track] = []
        by_id: dict[int, Track] = {}
        for a in assets:
            loc = locs.get(a["location_id"])
            portable: str = a["portable_id"]
            streaming = bool(loc and loc.streaming) or bool(STREAMING_URI.match(portable))
            if streaming:
                path = portable
            else:
                root = (loc.root if loc else None) or _guess_volume_root(portable) or "/"
                path = portable if portable.startswith("/") else root + portable
            cam = parse_key(a["key"]) or _comment_key(a["comments"])
            artist, title = split_artist_title(a["artist"], a["name"])
            plays = a["dj_play_count"] if a["dj_play_count"] is not None else 0
            if streaming:
                m = re.match(r"^streaming://([a-z]+)", portable)
                source = f"streaming:{m.group(1) if m else 'unknown'}"
            else:
                source = "itunes" if a["third_party_type"] == 1 else "local"
            color = a["color"]
            t = Track(
                id=a["id"],
                path=path,
                storedPath=portable,
                root="streaming" if streaming else ((loc.root if loc else None) or "/"),
                source=source,
                fileType=a["format"] or None,
                title=title,
                artist=artist,
                album=a["album"] or None,
                genre=a["genre"] or None,
                comment=a["comments"] or None,
                grouping=a["grouping"] or None,
                remixer=a["remixer"] or None,
                composer=a["composer"] or None,
                label=a["label"] or None,
                year=a["year"] or None,
                bpm=js_round(a["bpm"] * 100) / 100 if (a["bpm"] or 0) > 0 else None,
                key=a["key"] or None,
                camelot=camelot_string(cam) if cam else None,
                keyName=key_name(cam) if cam else None,
                energy=parse_energy(a["comments"], a["grouping"]),
                durationSec=a["length_ms"] / 1000 if a["length_ms"] else None,
                bitrate=f"{js_round(a['file_bit_rate'] / 1000)}kbps" if a["file_bit_rate"] else None,
                sampleRate=f"{_num_str(a['file_sample_rate'] / 1000)}k" if a["file_sample_rate"] else None,
                sizeBytes=a["file_size"],
                color=f"#{int(color) & 0xFFFFFF:06x}" if isinstance(color, (int, float)) else None,
                rating=js_round(a["rating"] * 5) if isinstance(a["rating"], (int, float)) else None,
                playCount=plays,
                addedAt=iso(a["time_added"]) if a["time_added"] else None,
                played=bool(a["dj_recently_played"]) or plays > 0,
                missing=False if streaming else not os.path.exists(path),
                corrupt=bool(a["is_corrupt"]),
                beatgridLocked=bool(a["is_beatgrid_locked"]),
            )
            tracks.append(t)
            by_id[a["id"]] = t

        containers = [dict(r) for r in db.execute("SELECT id, parent_id, name, type, list_order FROM container")]
        c_by_id = {c["id"]: c for c in containers}
        filenames = {
            r["container_id"]: r["filename"]
            for r in db.execute("SELECT container_id, filename FROM dj_container_metadata")
        }
        rules = {r["container_id"]: r["rules"] for r in db.execute("SELECT container_id, rules FROM smart_crate_rules")}
        by_container: dict[int, list[int]] = {}
        for m in db.execute(
            """SELECT lc.container_id AS cid, ca.asset_id AS aid FROM container_asset ca
               JOIN location_container lc ON lc.id = ca.location_container_id
               ORDER BY lc.container_id, ca.list_order, ca.id"""
        ):
            by_container.setdefault(m["cid"], []).append(m["aid"])

        def path_of(c: dict) -> tuple[list[str], str]:
            names: list[str] = []
            cur: dict | None = c
            while cur and cur["type"] != 0:
                names.insert(0, clean_name(cur["name"]))
                cur = c_by_id.get(cur["parent_id"]) if cur["parent_id"] is not None else None
            space = re.sub(r" root$", "", cur["name"]) if cur else ""
            return names, space

        crates: list[Crate] = []
        ordered = sorted(
            containers,
            key=lambda c: (c["parent_id"] if c["parent_id"] is not None else -1, c["list_order"] or 0),
        )
        for c in ordered:
            if c["type"] == 0:
                continue
            names, space = path_of(c)
            kind = CONTAINER_KIND.get(c["type"], "playlist")
            # Internal system containers (e.g. the Stems smart crate) carry an id suffix and no user name.
            if space == "Stems":
                continue
            is_serato = space == "Serato Library"
            name = " / ".join(names) if is_serato else f"{space}: {' / '.join(names)}"
            paths = [by_id[i].path for i in by_container.get(c["id"], []) if i in by_id and by_id[i].path]
            file_name = filenames.get(c["id"])
            if file_name is None:
                file_name = "%%".join(names) + ".crate" if is_serato and kind == "crate" else ""
            crates.append(
                Crate(
                    id=c["id"],
                    name=name,
                    kind=kind,
                    space=space,
                    fileName=file_name,
                    file="",
                    root=sqlite_dir(),
                    smart=kind == "smart crate",
                    rules=_safe_json(rules[c["id"]]) if c["id"] in rules else None,
                    trackPaths=paths,
                )
            )
        by_path: dict[str, Track] = {}
        for t in tracks:
            by_path[t.path] = t  # last wins, like new Map(entries)
        for c in crates:
            for p in c.trackPaths:
                t = by_path.get(p)
                if t and c.name not in t.crates:
                    t.crates.append(c.name)
        return tracks, crates


def _num_str(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def load_sqlite_sessions(limit: int = 200) -> list[Session]:
    with closing(_open()) as db:
        sessions = db.execute(
            "SELECT id, start_time, end_time, name, notes FROM history_session ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        entries = db.execute(
            """SELECT session_id, portable_id, artist, name, bpm, key, start_time, end_time, played, deck, device,
                      location_id
               FROM history_entry WHERE session_id IN (SELECT id FROM history_session ORDER BY start_time DESC LIMIT ?)
               ORDER BY start_time, id""",
            (limit,),
        ).fetchall()
        locs = _locations(db)
    grouped: dict[int, list[HistoryEntry]] = {}
    for e in entries:
        loc = locs.get(e["location_id"])
        portable = e["portable_id"] or ""
        if STREAMING_URI.match(portable) or (loc and loc.streaming):
            path = portable
        else:
            path = ((loc.root if loc else None) or "/") + portable
        start, end = e["start_time"] or 0, e["end_time"] or 0
        deck = e["deck"]
        try:
            deck_n = int(float(deck)) if deck else None
        except (TypeError, ValueError):
            deck_n = None
        grouped.setdefault(e["session_id"], []).append(
            HistoryEntry(
                session=str(e["session_id"]),
                path=path if portable else None,
                title=e["name"] or None,
                artist=e["artist"] or None,
                bpm=_num_str(js_round(e["bpm"] * 100) / 100) if e["bpm"] else None,
                key=e["key"] or None,
                deck=deck_n or None,
                startTime=iso(start) if start > 0 else None,
                endTime=iso(end) if end > 0 else None,
                playTimeSec=end - start if end > 0 and start > 0 else None,
                played=bool(e["played"]),
                device=e["device"] or None,
            )
        )
    master_mtime = iso(os.stat(master_path()).st_mtime)
    out: list[Session] = []
    for s in sessions:
        items = grouped.get(s["id"], [])
        last = max((parse_iso(e.endTime or e.startTime) for e in items), default=0)
        out.append(
            Session(
                file=master_path(),
                id=s["id"],
                name=f"{s['name']} (#{s['id']})" if s["name"] else f"Session {s['id']}",
                startedAt=iso(s["start_time"]) if s["start_time"] else None,
                endedAt=iso(s["end_time"]) if s["end_time"] else None,
                modifiedAt=iso(last / 1000) if last else master_mtime,
                notes=s["notes"] or None,
                entries=items,
            )
        )
    return out


def sqlite_lock_owner() -> str | None:
    """Is Serato's live library locked by a running Serato process?"""
    if not has_sqlite_library():
        return None
    try:
        with closing(_open()) as db:
            r = db.execute("SELECT owner_process_name AS name, owner_process_id AS pid FROM lock").fetchone()
            return f"{r['name']} (pid {r['pid']})" if r else None
    except sqlite3.Error:
        return None


def sqlite_signature() -> str:
    parts = []
    for f in ("master.sqlite", "master.sqlite-wal"):
        try:
            st = os.stat(os.path.join(sqlite_dir(), f))
            parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f + ":none")
    return "|".join(parts)

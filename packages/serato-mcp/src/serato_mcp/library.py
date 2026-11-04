"""Read model over one or more `_Serato_` folders (tracks from `database V2`, crates from `Subcrates/`,
smart crates and play history), or over Serato DJ 4's SQLite library when it exists."""

from __future__ import annotations

import os
import re
import struct
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

from .binary import Field, decode, decode_utf16be, get_child, read_value
from .jsnum import iso, parse_float
from .keys import camelot_string, key_name, parse_key
from .models import Crate, HistoryEntry, SeratoRoot, Session, Track
from .sqlite import (
    has_sqlite_library,
    load_sqlite,
    load_sqlite_sessions,
    parse_energy,
    split_artist_title,
    sqlite_signature,
)


def _warn(msg: str) -> None:
    print(f"serato-mcp: {msg}", file=sys.stderr)


# ---- roots ---------------------------------------------------------------------


def discover_roots() -> list[SeratoRoot]:
    roots: list[SeratoRoot] = []

    def add(d: str, volume_root: str) -> None:
        has_files = os.path.exists(os.path.join(d, "database V2")) or os.path.exists(os.path.join(d, "Subcrates"))
        if has_files and not any(r.dir == d for r in roots):
            roots.append(SeratoRoot(d, volume_root))

    env = os.environ.get("SERATO_ROOTS")
    if env:
        for entry in filter(None, env.split(":")):
            d, _, vol = entry.partition("=")  # "dir" or "dir=volumeRoot"
            add(d, vol or "/")
        return roots
    add(str(Path.home() / "Music" / "_Serato_"), "/")
    if os.path.exists("/Volumes"):
        for v in os.listdir("/Volumes"):
            vol = os.path.join("/Volumes", v)
            try:
                if os.path.isdir(vol):
                    add(os.path.join(vol, "_Serato_"), vol + "/")
            except OSError:
                pass
    return roots


def to_absolute(root: SeratoRoot, stored: str) -> str:
    if stored.startswith("/"):
        return stored
    return os.path.normpath(os.path.join(root.volumeRoot, stored))


def to_stored(root: SeratoRoot, absolute: str) -> str:
    if root.volumeRoot == "/":
        return absolute.removeprefix("/")
    if not absolute.startswith(root.volumeRoot):
        raise ValueError(
            f"{absolute} is not on the volume of {root.dir}; Serato can only reference files on the same drive "
            "as the _Serato_ folder"
        )
    return absolute[len(root.volumeRoot) :]


def root_for_path(roots: list[SeratoRoot], absolute: str) -> SeratoRoot:
    candidates = [r for r in roots if r.volumeRoot == "/" or absolute.startswith(r.volumeRoot)]
    candidates.sort(key=lambda r: -len(r.volumeRoot))
    if not candidates:
        raise ValueError(f"No Serato library found for {absolute}")
    return candidates[0]


# ---- parsing ---------------------------------------------------------------------


def _parse_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", s.strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _color(n: object) -> str | None:
    return f"#{n & 0xFFFFFF:06x}" if isinstance(n, int) and not isinstance(n, bool) else None


def _track_from_field(root: SeratoRoot, f: Field) -> Track | None:
    v: dict[str, object] = {c.tag: read_value(c) for c in f.children or []}
    stored = v.get("pfil")
    if not stored or not isinstance(stored, str):
        return None
    path = to_absolute(root, stored)
    raw_key = v.get("tkey") or None
    m = re.match(r"^\s*(\d{1,2}[AB])\b", str(v.get("tcom") or ""), re.I)
    cam = parse_key(raw_key) or (parse_key(m.group(1)) if m else None)  # type: ignore[arg-type]
    bpm = parse_float(v.get("tbpm"))
    artist, title = split_artist_title(v.get("tart"), v.get("tsng"))  # type: ignore[arg-type]
    added = v["uadd"] if isinstance(v.get("uadd"), int) else parse_float(v.get("tadd"))

    def text(tag: str) -> str | None:
        value = v.get(tag)
        return value if isinstance(value, str) and value else None

    return Track(
        path=path,
        storedPath=stored,
        root=root.dir,
        fileType=v.get("ttyp") if isinstance(v.get("ttyp"), str) else None,
        title=title,
        artist=artist,
        album=text("talb"),
        genre=text("tgen"),
        comment=text("tcom"),
        grouping=text("tgrp"),
        remixer=text("trmx"),
        composer=text("tcmp"),
        label=text("tlbl"),
        year=text("ttyr"),
        bpm=bpm if bpm == bpm and bpm not in (float("inf"),) and bpm > 0 else None,
        key=raw_key if isinstance(raw_key, str) else None,
        camelot=camelot_string(cam) if cam else None,
        keyName=key_name(cam) if cam else None,
        energy=parse_energy(text("tcom"), text("tgrp")),
        durationSec=_parse_duration(v.get("tlen") if isinstance(v.get("tlen"), str) else None),  # type: ignore[arg-type]
        bitrate=v.get("tbit") if isinstance(v.get("tbit"), str) else None,
        sampleRate=v.get("tsmp") if isinstance(v.get("tsmp"), str) else None,
        sizeBytes=v.get("ufsb") if isinstance(v.get("ufsb"), int) else None,  # type: ignore[arg-type]
        color=_color(v.get("ulbl")),
        addedAt=iso(added) if added == added and added > 0 else None,  # type: ignore[operator]
        played=v.get("bply") is True,
        missing=not os.path.exists(path),
        corrupt=v.get("bcrt") is True,
        beatgridLocked=v.get("bbgl") is True,
    )


def crate_display_name(file_name: str) -> str:
    return " / ".join(re.sub(r"\.s?crate$", "", file_name).split("%%"))


def crate_file_name(display_name: str) -> str:
    parts = [p.strip() for p in display_name.split("/") if p.strip()]
    if not parts:
        raise ValueError("Crate name is empty")
    for p in parts:
        if re.search(r"[\\:%]", p):
            raise ValueError(f'Crate name part "{p}" contains an unsupported character (\\ : %)')
    return "%%".join(parts) + ".crate"


def _parse_crate(root: SeratoRoot, file: str, smart: bool) -> Crate:
    with open(file, "rb") as fh:
        fields = decode(fh.read())
    track_paths: list[str] = []
    columns: list[str] = []
    for f in fields:
        if f.tag == "otrk":
            p = get_child(f, "ptrk")
            if p:
                track_paths.append(to_absolute(root, decode_utf16be(p.raw)))
        elif f.tag == "ovct":
            n = get_child(f, "tvcn")
            if n:
                columns.append(decode_utf16be(n.raw))
    file_name = os.path.basename(file)
    return Crate(
        name=crate_display_name(file_name),
        fileName=file_name,
        file=file,
        root=root.dir,
        smart=smart,
        trackPaths=track_paths,
        columns=columns,
    )


# ---- history -----------------------------------------------------------------------

# Serato history `adat` records use numeric field ids instead of ASCII tags.
ADAT_FIELDS = {
    1: ("row", "u32"), 2: ("path", "str"), 3: ("location", "str"), 4: ("filename", "str"), 6: ("title", "str"),
    7: ("artist", "str"), 15: ("bpm", "str"), 28: ("startTime", "u32"), 29: ("endTime", "u32"), 31: ("deck", "u32"),
    45: ("playTimeSec", "u32"), 50: ("played", "bool"), 51: ("key", "str"),
}  # fmt: skip
HISTORY_KEYS = set(HistoryEntry.__dataclass_fields__)


def _parse_adat(buf: bytes, session: str) -> HistoryEntry:
    e: dict[str, object] = {}
    pos = 0
    while pos + 8 <= len(buf):
        fid, length = struct.unpack_from(">II", buf, pos)
        raw = buf[pos + 8 : pos + 8 + length]
        pos += 8 + length
        spec = ADAT_FIELDS.get(fid)
        if not spec:
            continue
        name, kind = spec
        if kind == "str":
            e[name] = re.sub(r"\0+$", "", decode_utf16be(raw))
        elif kind == "u32" and len(raw) == 4:
            e[name] = struct.unpack(">I", raw)[0]
        elif kind == "bool" and len(raw) >= 1:
            e[name] = raw[0] != 0
    for k in ("startTime", "endTime"):
        n = e.pop(k, None)
        if isinstance(n, int) and n > 0:
            e[k] = iso(n)
    return HistoryEntry(session=session, **{k: v for k, v in e.items() if k in HISTORY_KEYS})  # type: ignore[arg-type]


def parse_session_file(file: str) -> Session:
    with open(file, "rb") as fh:
        buf = fh.read()
    name = re.sub(r"\.session$", "", os.path.basename(file))
    entries: list[HistoryEntry] = []
    pos = 0
    while pos + 8 <= len(buf):
        tag = buf[pos : pos + 4].decode("latin1")
        (length,) = struct.unpack_from(">I", buf, pos + 4)
        body = buf[pos + 8 : pos + 8 + length]
        pos += 8 + length
        if tag != "oent":
            continue
        p = 0
        while p + 8 <= len(body):
            t = body[p : p + 4].decode("latin1")
            (sub_len,) = struct.unpack_from(">I", body, p + 4)
            if t == "adat":
                entries.append(_parse_adat(body[p + 8 : p + 8 + sub_len], name))
            p += 8 + sub_len
    starts = sorted(e.startTime for e in entries if e.startTime)
    return Session(
        file=file,
        name=name,
        startedAt=starts[0] if starts else None,
        modifiedAt=iso(os.stat(file).st_mtime),
        entries=entries,
    )


# ---- library -----------------------------------------------------------------------


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = re.sub("[̀-ͯ]", "", s).lower()
    return re.sub(r"[^a-z0-9#]+", " ", s).strip()


def is_streaming(path: str) -> bool:
    return bool(re.match(r"^[a-z]+://", path))


class Library:
    def __init__(self, roots: list[SeratoRoot] | None = None):
        self.roots = discover_roots() if roots is None else roots
        self.tracks: list[Track] = []
        self.crates: list[Crate] = []
        self._by_path: dict[str, Track] = {}
        self._signature = ""

    @property
    def backend(self) -> str:
        """ "sqlite" for Serato DJ 4+ (live library), "legacy" for database V2 + crate files."""
        return "sqlite" if has_sqlite_library() else "legacy"

    def refresh(self, force: bool = False) -> Library:
        """Reload when any Serato file changed since the last read."""
        sig = self._compute_signature()
        if not force and sig == self._signature:
            return self
        self._signature = sig
        self._by_path = {}
        legacy_tracks, legacy_crates = self.load_legacy()
        sqlite = self.backend == "sqlite"
        if sqlite:
            self.tracks, self.crates = load_sqlite()
            # Crates written by serato-mcp (or Serato 3) that Serato 4 hasn't imported yet.
            known = {c.name.lower() for c in self.crates if c.space == "Serato Library"}
            for c in legacy_crates:
                twin = next(
                    (x for x in self.crates if x.space == "Serato Library" and x.name.lower() == c.name.lower()), None
                )
                if twin:
                    twin.file = c.file
                elif c.name.lower() not in known:
                    self.crates.append(replace(c, kind="crate", space="Serato Library", pendingImport=True))
        else:
            self.tracks, self.crates = legacy_tracks, legacy_crates
        for t in self.tracks:
            self._by_path.setdefault(t.path, t)
        for c in self.crates:
            if not c.pendingImport and sqlite:
                continue  # memberships already set
            for p in c.trackPaths:
                t = self._by_path.get(p)
                if t and c.name not in t.crates:
                    t.crates.append(c.name)
        return self

    def load_legacy(self) -> tuple[list[Track], list[Crate]]:
        """Tracks and crates from the legacy `_Serato_` files (database V2, Subcrates)."""
        tracks: list[Track] = []
        crates: list[Crate] = []
        seen: set[str] = set()
        for root in self.roots:
            db = os.path.join(root.dir, "database V2")
            if os.path.exists(db):
                with open(db, "rb") as fh:
                    fields = decode(fh.read())
                for f in fields:
                    if f.tag != "otrk":
                        continue
                    t = _track_from_field(root, f)
                    if t and t.path not in seen:
                        seen.add(t.path)
                        tracks.append(t)
            for sub, smart in (("Subcrates", False), ("SmartCrates", True)):
                d = os.path.join(root.dir, sub)
                if not os.path.exists(d):
                    continue
                for file in _listdir(d):
                    if not re.search(r"\.s?crate$", file):
                        continue
                    try:
                        crate = _parse_crate(root, os.path.join(d, file), smart)
                        crate.kind = "smart crate" if smart else "crate"
                        crates.append(crate)
                    except (OSError, ValueError) as err:
                        _warn(f"could not read crate {file}: {err}")
        return tracks, crates

    def _compute_signature(self) -> str:
        parts = [sqlite_signature() if has_sqlite_library() else "legacy"]
        for r in self.roots:
            for p in (
                os.path.join(r.dir, "database V2"),
                os.path.join(r.dir, "Subcrates"),
                os.path.join(r.dir, "SmartCrates"),
            ):
                try:
                    st = os.stat(p)
                    parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
                    if os.path.isdir(p):
                        parts.extend(f"{f}:{os.stat(os.path.join(p, f)).st_mtime_ns}" for f in _listdir(p))
                except OSError:
                    parts.append(p + ":none")
        return "|".join(parts)

    def track(self, path: str) -> Track | None:
        return self._by_path.get(path)

    def playable(self, include_streaming: bool = False, include_missing: bool = False) -> list[Track]:
        """Tracks a DJ can load: local files that exist, plus streaming tracks when asked."""
        out = []
        for t in self.tracks:
            if t.source and t.source.startswith("streaming"):
                if include_streaming:
                    out.append(t)
            elif "/_Serato_/Imported/" in t.path:
                continue  # bundled samples
            elif include_missing or not t.missing:
                out.append(t)
        return out

    def resolve(self, ref: str) -> Track:
        """Resolve a user reference: absolute path, stored path, or a fuzzy "artist - title" query."""
        exact = self._by_path.get(ref) or next(
            (t for t in self.tracks if t.storedPath == ref or "/" + t.storedPath == ref), None
        )
        if exact:
            return exact
        hits = self.search(ref, 2)
        if not hits:
            raise ValueError(f'No track matches "{ref}"')
        return hits[0]

    def search(self, query: str, limit: int = 50, pool: list[Track] | None = None) -> list[Track]:
        pool = self.tracks if pool is None else pool
        terms = [w for w in normalize(query).split(" ") if w]
        if not terms:
            return pool[:limit]
        joined = " ".join(terms)
        scored: list[tuple[Track, int]] = []
        for t in pool:
            base = os.path.basename(t.path)
            title = normalize(t.title or base)
            artist = normalize(t.artist or "")
            hay = " ".join(
                [artist, title, normalize(t.album or ""), normalize(t.genre or ""), normalize(t.remixer or ""),
                 normalize(t.label or ""), normalize(base)]
            )  # fmt: skip
            if not all(w in hay for w in terms):
                continue
            s = sum((3 if w in title else 0) + (2 if w in artist else 0) for w in terms)
            if title.startswith(joined):
                s += 2
            if t.missing:
                s -= 1
            if t.source and t.source.startswith("streaming"):
                s -= 2
            scored.append((t, s))
        scored.sort(key=lambda x: -x[1])
        return [t for t, _ in scored[:limit]]

    def crate(self, name: str) -> Crate:
        n = name.strip().lower()
        c = next(
            (c for c in self.crates if c.name.lower() == n or c.fileName.lower() in (n, n + ".crate")), None
        ) or next((c for c in self.crates if c.name.lower().endswith("/ " + n)), None)
        if not c:
            raise ValueError(f'No crate named "{name}". Crates: {", ".join(c.name for c in self.crates)}')
        return c

    def sessions(self) -> list[Session]:
        if self.backend == "sqlite":
            return load_sqlite_sessions()
        out: list[Session] = []
        for r in self.roots:
            d = os.path.join(r.dir, "History", "Sessions")
            if not os.path.exists(d):
                continue
            for f in _listdir(d):
                if not f.endswith(".session"):
                    continue
                try:
                    out.append(parse_session_file(os.path.join(d, f)))
                except (OSError, ValueError, struct.error) as err:
                    _warn(f"could not read session {f}: {err}")
        out.sort(key=lambda s: s.startedAt or s.modifiedAt, reverse=True)
        return out


def _listdir(d: str) -> list[str]:
    """Directory entries in the order Node's readdirSync returns them on macOS (sorted)."""
    return sorted(os.listdir(d))

"""Serato (and Mixed In Key) metadata stored inside audio files.

MP3/AIFF/WAV: ID3 GEOB frames ("Serato Markers2", "Serato BeatGrid", "Serato Autotags", plus Mixed In
Key's base64-JSON "Key"/"Energy"). MP4: "----:com.serato.dj:*" atoms. FLAC/Ogg: SERATO_* vorbis comments.
The MP4/FLAC forms wrap the same payload in base64 with a small mime header.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass, field

from .jsnum import parse_float, round_to


@dataclass
class CuePoint:
    index: int  # 0-based hot cue slot
    positionMs: int
    color: str  # #rrggbb
    name: str


@dataclass
class Loop:
    index: int
    startMs: int
    endMs: int
    locked: bool
    name: str


@dataclass
class BeatGridMarker:
    positionSec: float
    bpm: float | None = None  # terminal marker
    beatsUntilNext: int | None = None  # non-terminal markers


@dataclass
class FileAnalysis:
    durationSec: float | None = None
    cues: list[CuePoint] = field(default_factory=list)
    loops: list[Loop] = field(default_factory=list)
    trackColor: str | None = None
    bpmLocked: bool | None = None
    beatGrid: list[BeatGridMarker] = field(default_factory=list)
    autoBpm: float | None = None
    autoGainDb: float | None = None
    mikKey: str | None = None
    mikEnergy: float | None = None
    tagBpm: float | None = None
    tagKey: str | None = None
    comment: str | None = None


def _hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _cstring(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.find(b"\0", pos)
    stop = len(buf) if end == -1 else end
    return buf[pos:stop].decode("utf-8", errors="replace"), stop + 1


def _b64(text: str) -> bytes:
    clean = re.sub(r"[^A-Za-z0-9+/]", "", text)
    # Serato sometimes writes base64 whose length is 1 mod 4; drop the stray char.
    if len(clean) % 4 == 1:
        clean = clean[:-1]
    return base64.b64decode(clean + "=" * (-len(clean) % 4))


def parse_markers2(payload: bytes) -> dict:
    """Markers2 GEOB payload: 0x01 0x01 then base64 of (0x01 0x01 + entries)."""
    out: dict = {"cues": [], "loops": [], "trackColor": None, "bpmLocked": None}
    if len(payload) < 3:
        return out
    text_end = payload.find(b"\0", 2)
    decoded = _b64(payload[2 : len(payload) if text_end == -1 else text_end].decode("latin1"))
    pos = 2  # skip inner version
    while pos < len(decoded):
        name, after = _cstring(decoded, pos)
        if not name or after + 4 > len(decoded):
            break
        (length,) = struct.unpack_from(">I", decoded, after)
        data = decoded[after + 4 : after + 4 + length]
        pos = after + 4 + length
        if name == "CUE" and len(data) >= 13:
            out["cues"].append(
                CuePoint(
                    data[1],
                    struct.unpack_from(">I", data, 2)[0],
                    _hex(data[7], data[8], data[9]),
                    _cstring(data, 12)[0],
                )
            )
        elif name == "LOOP" and len(data) >= 20:
            start, end = struct.unpack_from(">II", data, 2)
            out["loops"].append(Loop(data[1], start, end, data[19] != 0, _cstring(data, 20)[0]))
        elif name == "COLOR" and len(data) >= 4:
            out["trackColor"] = _hex(data[1], data[2], data[3])
        elif name == "BPMLOCK" and len(data) >= 1:
            out["bpmLocked"] = data[0] != 0
    out["cues"].sort(key=lambda c: c.index)
    out["loops"].sort(key=lambda lp: lp.index)
    return out


def parse_beatgrid(payload: bytes) -> list[BeatGridMarker]:
    if len(payload) < 6:
        return []
    (count,) = struct.unpack_from(">I", payload, 2)
    markers: list[BeatGridMarker] = []
    pos = 6
    i = 0
    while i < count and pos + 8 <= len(payload):
        (position,) = struct.unpack_from(">f", payload, pos)
        if i == count - 1:
            markers.append(BeatGridMarker(position, bpm=round_to(struct.unpack_from(">f", payload, pos + 4)[0], 3)))
        else:
            markers.append(BeatGridMarker(position, beatsUntilNext=struct.unpack_from(">I", payload, pos + 4)[0]))
        i += 1
        pos += 8
    return markers


def parse_autotags(payload: bytes) -> dict:
    parts: list[str] = []
    pos = 2
    while pos < len(payload):
        s, pos = _cstring(payload, pos)
        parts.append(s)
    bpm = parse_float(parts[0]) if parts else float("nan")
    gain = parse_float(parts[2]) if len(parts) > 2 else float("nan")
    return {
        "autoBpm": bpm if bpm > 0 else None,
        "autoGainDb": gain if gain == gain and abs(gain) != float("inf") else None,
    }


def _unwrap_envelope(text: str) -> bytes:
    """MP4 / FLAC wrap payloads as base64("application/octet-stream\\0\\0<name>\\0" + payload)."""
    raw = _b64(text)
    prefix = b"application/octet-stream"
    if not raw.startswith(prefix):
        return raw
    pos = len(prefix)
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    return raw[raw.find(b"\0", pos) + 1 :]


def _mik_json(data: bytes) -> dict | None:
    try:
        value = json.loads(_b64(data.decode("latin1")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


MP4_KEYS = {"markersv2": "Serato Markers2", "beatgrid": "Serato BeatGrid", "autgain": "Serato Autotags"}
VORBIS_KEYS = {
    "SERATO_MARKERS_V2": "Serato Markers2",
    "SERATO_BEATGRID": "Serato BeatGrid",
    "SERATO_AUTOTAGS": "Serato Autotags",
}


def read_file_analysis(path: str) -> FileAnalysis:
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    f = mutagen.File(path)
    if f is None:
        raise ValueError("unsupported file type")
    res = FileAnalysis()
    length = getattr(getattr(f, "info", None), "length", None)
    res.durationSec = round_to(length) if length else None

    geob: dict[str, bytes] = {}
    tags = f.tags
    comments: list[str] = []
    if isinstance(tags, ID3):
        for frame in tags.getall("GEOB"):
            if frame.desc:
                geob[frame.desc] = bytes(frame.data)
        if tags.getall("TBPM"):
            bpm = parse_float(str(tags.getall("TBPM")[0]))
            res.tagBpm = bpm if bpm == bpm else None
        if tags.getall("TKEY"):
            res.tagKey = str(tags.getall("TKEY")[0]) or None
        comments = [t for c in tags.getall("COMM") for t in c.text if t]
    elif isinstance(tags, MP4Tags):
        for key, values in tags.items():
            if key.startswith("----:com.serato.dj:") and values:
                name = MP4_KEYS.get(key[len("----:com.serato.dj:") :].lower())
                if name:
                    text = bytes(values[0]).decode("latin1")
                    is_m2 = name == "Serato Markers2"
                    geob[name] = b"\x01\x01" + text.encode("latin1") if is_m2 else _unwrap_envelope(text)
        if tags.get("tmpo"):
            res.tagBpm = float(tags["tmpo"][0])
        comments = [str(c) for c in tags.get("\xa9cmt", []) if c]
    elif tags is not None and hasattr(tags, "keys"):  # vorbis comments (FLAC, Ogg)
        for key in tags.keys():  # noqa: SIM118 - vorbis comment objects iterate as (key, value) pairs
            name = VORBIS_KEYS.get(key.upper())
            if name and tags[key]:
                geob[name] = _unwrap_envelope(str(tags[key][0]))
        for key in ("bpm", "BPM"):
            if key in tags:
                bpm = parse_float(tags[key][0])
                res.tagBpm = bpm if bpm == bpm else None
        for key in ("initialkey", "key"):
            if key in tags and tags[key]:
                res.tagKey = str(tags[key][0])
                break
        comments = [str(c) for k in ("comment", "description") if k in tags for c in tags[k] if c]
    res.comment = " | ".join(comments) or None

    if "Serato Markers2" in geob:
        m2 = parse_markers2(geob["Serato Markers2"])
        res.cues, res.loops, res.trackColor, res.bpmLocked = m2["cues"], m2["loops"], m2["trackColor"], m2["bpmLocked"]
    if "Serato BeatGrid" in geob:
        res.beatGrid = parse_beatgrid(geob["Serato BeatGrid"])
    if "Serato Autotags" in geob:
        at = parse_autotags(geob["Serato Autotags"])
        res.autoBpm, res.autoGainDb = at["autoBpm"], at["autoGainDb"]
    mik_key = _mik_json(geob["Key"]) if "Key" in geob else None
    if mik_key and isinstance(mik_key.get("key"), str):
        res.mikKey = mik_key["key"]
    mik_energy = _mik_json(geob["Energy"]) if "Energy" in geob else None
    if mik_energy and isinstance(mik_energy.get("energyLevel"), (int, float)):
        res.mikEnergy = mik_energy["energyLevel"]
    return res

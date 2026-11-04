"""Library records. Field names match the JSON the tools return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SeratoRoot:
    dir: str  # absolute path of the `_Serato_` folder
    volumeRoot: str  # paths stored in this root's files are relative to this directory


@dataclass
class Track:
    path: str  # absolute path, or streaming:// URI
    storedPath: str  # as written in Serato's files
    root: str  # `_Serato_` dir it belongs to
    id: int | None = None  # Serato 4 asset id
    source: str | None = None  # local | itunes | streaming:spotify ...
    fileType: str | None = None
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
    bpm: float | None = None
    key: str | None = None  # as stored by Serato
    camelot: str | None = None
    keyName: str | None = None
    energy: int | None = None  # 1..10 from a Mixed In Key comment, when present
    durationSec: float | None = None
    bitrate: str | None = None
    sampleRate: str | None = None
    sizeBytes: int | None = None
    color: str | None = None  # #rrggbb
    addedAt: str | None = None  # ISO
    rating: int | None = None  # 0-5
    playCount: int | None = None
    played: bool = False
    missing: bool = False  # file not found on disk right now
    corrupt: bool = False
    beatgridLocked: bool = False
    crates: list[str] = field(default_factory=list)

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


@dataclass
class Crate:
    name: str  # display path, nested with " / "
    fileName: str  # e.g. "Parent%%Child.crate"
    file: str
    root: str
    smart: bool
    trackPaths: list[str]  # absolute
    columns: list[str] = field(default_factory=list)
    id: int | None = None
    kind: str | None = None  # crate | smart crate | playlist | playlist folder | streaming folder | streaming playlist
    space: str | None = None  # Serato Library, iTunes, Spotify...
    rules: Any = None  # smart crate rules
    pendingImport: bool | None = None  # written to the legacy crate files but not yet imported by Serato 4


@dataclass
class HistoryEntry:
    session: str
    path: str | None = None
    title: str | None = None
    artist: str | None = None
    bpm: str | None = None
    key: str | None = None
    deck: int | None = None
    startTime: str | None = None
    endTime: str | None = None
    playTimeSec: int | None = None
    played: bool | None = None
    device: str | None = None


@dataclass
class Session:
    file: str
    name: str
    modifiedAt: str
    entries: list[HistoryEntry]
    id: int | None = None
    startedAt: str | None = None
    endedAt: str | None = None
    notes: str | None = None

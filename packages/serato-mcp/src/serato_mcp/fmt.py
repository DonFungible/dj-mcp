"""Output shaping: compact track records, M3U export, and JSON that matches what the tools always returned."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import unicodedata
from typing import Any

from .jsnum import js_round, num
from .models import Track
from .planner import _Null, mmss


def brief(t: Track) -> dict:
    """Compact track shape for lists."""
    out: dict[str, Any] = {
        "path": t.path,
        "artist": t.artist,
        "title": t.title,
        "bpm": t.bpm,
        "key": f"{t.camelot} ({t.keyName})" if t.camelot else t.key,
        "energy": t.energy,
        "genre": t.genre,
        "length": mmss(t.durationSec) if t.durationSec else None,
    }
    if t.playCount:
        out["plays"] = t.playCount
    if t.source and t.source != "local":
        out["source"] = t.source
    if t.missing:
        out["missing"] = True
    if t.crates:
        out["crates"] = t.crates
    return out


def write_m3u(file: str, tracks: list[Track]) -> str:
    lines = ["#EXTM3U"]
    for t in tracks:
        duration = js_round(t.durationSec if t.durationSec is not None else -1)
        lines.append(f"#EXTINF:{duration},{t.artist + ' - ' if t.artist else ''}{t.title or ''}")
        lines.append(t.path)
    os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
    with open(file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return file


def clean(value: Any) -> Any:
    """Plain JSON data. None drops a key (like undefined in JavaScript); NULL is an explicit null."""
    if isinstance(value, _Null):
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return num(value)
    return value


def to_json(value: Any) -> str:
    return json.dumps(clean(value), indent=2, ensure_ascii=False)


def locale_key(s: str) -> tuple[str, str]:
    """Rough stand-in for String.localeCompare: case- and accent-insensitive first, lowercase before uppercase."""
    base = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return base.casefold(), s.swapcase()


def expand_home(path: str) -> str:
    return re.sub(r"^~(?=/)", os.path.expanduser("~"), path)

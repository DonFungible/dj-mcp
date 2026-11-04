"""Musical key <-> Camelot wheel conversion and harmonic-mixing rules."""

from __future__ import annotations

import re

# Camelot code -> canonical key name (rekordbox uses flats for most keys).
_CAMELOT_TO_KEY = {
    "1A": "Abm",
    "1B": "B",
    "2A": "Ebm",
    "2B": "F#",
    "3A": "Bbm",
    "3B": "Db",
    "4A": "Fm",
    "4B": "Ab",
    "5A": "Cm",
    "5B": "Eb",
    "6A": "Gm",
    "6B": "Bb",
    "7A": "Dm",
    "7B": "F",
    "8A": "Am",
    "8B": "C",
    "9A": "Em",
    "9B": "G",
    "10A": "Bm",
    "10B": "D",
    "11A": "F#m",
    "11B": "A",
    "12A": "Dbm",
    "12B": "E",
}

_ENHARMONIC = {
    "G#": "Ab",
    "D#": "Eb",
    "A#": "Bb",
    "C#": "Db",
    "Gb": "F#",
    "Cb": "B",
    "Fb": "E",
    "E#": "F",
    "B#": "C",
}

_KEY_TO_CAMELOT = {v: k for k, v in _CAMELOT_TO_KEY.items()}

_CAMELOT_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([ABab])\s*$")
_KEY_RE = re.compile(r"^\s*([A-Ga-g])([#b♯♭]?)\s*(m|min|minor|maj|major)?\s*$")


def to_camelot(key: str | None) -> str | None:
    """Convert a key in classical ("Am", "G#m", "Eb") or Camelot ("8A") notation to Camelot."""
    if not key:
        return None
    m = _CAMELOT_RE.match(key)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = _KEY_RE.match(key)
    if not m:
        return None
    note = m.group(1).upper() + m.group(2).replace("♯", "#").replace("♭", "b")
    note = _ENHARMONIC.get(note, note)
    minor = (m.group(3) or "").lower() in ("m", "min", "minor")
    return _KEY_TO_CAMELOT.get(note + ("m" if minor else ""))


def key_name(camelot: str | None) -> str | None:
    return _CAMELOT_TO_KEY.get(camelot or "")


def compatible_camelot(code: str, energy_boost: bool = False) -> list[str]:
    """Camelot codes that mix harmonically with `code`.

    Same key, +/-1 on the wheel (same letter), and the relative major/minor.
    With energy_boost, also +2 (same letter) and the +7 "semitone" jump.
    """
    c = to_camelot(code)
    if c is None:
        return []
    num, letter = int(c[:-1]), c[-1]
    other = "B" if letter == "A" else "A"

    def wrap(n: int) -> int:
        return (n - 1) % 12 + 1

    out = [c, f"{wrap(num + 1)}{letter}", f"{wrap(num - 1)}{letter}", f"{num}{other}"]
    if energy_boost:
        out += [f"{wrap(num + 2)}{letter}", f"{wrap(num + 7)}{letter}"]
    return list(dict.fromkeys(out))


def bpm_compatible(a: float, b: float, tolerance_pct: float, allow_double_time: bool = True) -> bool:
    if not a or not b:
        return False
    candidates = [b, b * 2, b / 2] if allow_double_time else [b]
    return any(abs(c - a) / a * 100 <= tolerance_pct for c in candidates)

"""Musical key parsing and harmonic compatibility on the Camelot wheel.

Accepts Serato/standard names (Gmin, F#m, Bb, Ebmaj, D#min), Camelot (8A, 11B) and Open Key (1m, 6d).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .jsnum import js_round


@dataclass(frozen=True)
class Camelot:
    num: int  # 1..12
    letter: str  # "A" = minor, "B" = major


PITCH = {
    "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "FB": 4, "F": 5, "E#": 5,
    "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11,
}  # fmt: skip

MINOR_CAMELOT = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]
MAJOR_CAMELOT = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]
MINOR_NAMES = ["Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "Abm", "Am", "Bbm", "Bm"]
MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def parse_key(text: str | None) -> Camelot | None:
    if not text:
        return None
    s = text.strip().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"(\d{1,2})\s*([AB])", s, re.I)
    if m:
        n = int(m.group(1))
        return Camelot(n, m.group(2).upper()) if 1 <= n <= 12 else None
    m = re.fullmatch(r"(\d{1,2})\s*([md])", s, re.I)  # Open Key: 1m = 8A, 1d = 8B
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 12:
            return None
        return Camelot((n + 6) % 12 + 1, "A" if m.group(2).lower() == "m" else "B")
    m = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(maj(or)?|min(or)?|m|M)?", s)
    if not m:
        return None
    pc = PITCH.get(m.group(1).upper() + ("B" if m.group(2) == "b" else m.group(2)))
    if pc is None:
        return None
    q = m.group(3) or ""
    minor = bool(re.match(r"min", q, re.I)) or q == "m"
    return Camelot(MINOR_CAMELOT[pc], "A") if minor else Camelot(MAJOR_CAMELOT[pc], "B")


def camelot_string(c: Camelot) -> str:
    return f"{c.num}{c.letter}"


def key_name(c: Camelot) -> str:
    table = MINOR_CAMELOT if c.letter == "A" else MAJOR_CAMELOT
    return (MINOR_NAMES if c.letter == "A" else MAJOR_NAMES)[table.index(c.num)]


def _wrap(n: int) -> int:
    return (n - 1) % 12 + 1


@dataclass
class KeyRelation:
    score: float  # 0..1
    relation: str
    description: str


CLASH = KeyRelation(0.1, "clash", "Keys clash: avoid long blends")


def key_compatibility(a: Camelot | None, b: Camelot | None) -> KeyRelation:
    """How well track B's key follows track A's."""
    if not a or not b:
        return KeyRelation(0.5, "unknown", "Key unknown for one of the tracks")
    step = (b.num - a.num) % 12  # clockwise distance
    if a.letter == b.letter:
        return {
            0: KeyRelation(1, "same", "Same key: perfect harmonic match"),
            1: KeyRelation(0.92, "+1", "One step clockwise: smooth lift in energy"),
            11: KeyRelation(0.9, "-1", "One step counter-clockwise: smooth, slightly calmer"),
            2: KeyRelation(0.62, "+2", "Two steps clockwise: noticeable energy boost (whole-tone up)"),
            7: KeyRelation(0.58, "+7", "Semitone up (+7 on the wheel): classic energy-boost key change"),
            10: KeyRelation(0.45, "-2", "Two steps counter-clockwise: drops energy, usable in breakdowns"),
            5: KeyRelation(0.35, "-7", "Semitone down: energy drop, keep the overlap short"),
        }.get(step, CLASH)
    if step == 0:
        mood = (
            "Relative major: same notes, brighter mood"
            if a.letter == "A"
            else "Relative minor: same notes, darker mood"
        )
        return KeyRelation(0.88, "relative", mood)
    # Diagonal moves: nA -> (n+1)B and nB -> (n-1)A
    if (a.letter == "A" and step == 1) or (a.letter == "B" and step == 11):
        return KeyRelation(0.68, "diagonal", "Diagonal move: mood change with a gentle lift")
    if (a.letter == "A" and step == 3) or (a.letter == "B" and step == 9):
        return KeyRelation(0.4, "parallel", "Parallel major/minor (same root): dramatic mood switch")
    return CLASH


def compatible_keys(k: Camelot) -> list[dict]:
    """Keys that mix well out of `k`, best first."""
    pairs = [(c, key_compatibility(k, c)) for n in range(1, 13) for c in (Camelot(n, "A"), Camelot(n, "B"))]
    good = sorted((p for p in pairs if p[1].score >= 0.55), key=lambda p: -p[1].score)
    return [{"key": camelot_string(c), "relation": r.relation, "score": r.score} for c, r in good]


def shifted_key(k: Camelot, pct: float) -> Camelot:
    """The key a track lands in when played `pct` faster without key lock."""
    semitones = js_round(12 * math.log2(1 + pct / 100))
    return Camelot(_wrap(k.num + semitones * 7), k.letter)

"""Order tracks into a DJ set: harmonic (Camelot) moves, small tempo changes, and an energy curve.

Tempo stands in for energy, since rekordbox doesn't store an energy rating:
- build:    gradually rising BPM (warm-up into peak)
- peak:     rise to the middle, then come back down
- cooldown: gradually falling BPM
- smooth:   no direction; just the easiest transitions
"""

from __future__ import annotations

from dataclasses import dataclass

from .camelot import compatible_camelot, to_camelot

CURVES = ("build", "peak", "cooldown", "smooth")


@dataclass(frozen=True)
class SetTrack:
    id: str
    title: str
    artist: str
    bpm: float | None
    camelot: str | None
    extra: tuple = ()  # opaque passthrough (e.g. a Spotify URI), returned untouched


def tempo_diff_pct(a: float | None, b: float | None) -> float | None:
    """Smallest % tempo change from a to b, allowing half/double time."""
    if not a or not b:
        return None
    return min(abs(c - a) / a * 100 for c in (b, b * 2, b / 2))


def key_relation(a: str | None, b: str | None) -> str:
    a, b = to_camelot(a), to_camelot(b)
    if not a or not b:
        return "unknown"
    if a == b:
        return "same"
    if b in compatible_camelot(a):
        return "compatible"
    if b in compatible_camelot(a, energy_boost=True):
        return "energy boost"
    return "clash"


_KEY_COST = {"same": 0.0, "compatible": 0.3, "energy boost": 1.0, "unknown": 1.0, "clash": 2.5}


def transition_cost(a: SetTrack, b: SetTrack, direction: int) -> float:
    """direction: +1 wants rising tempo, -1 falling, 0 doesn't care."""
    cost = _KEY_COST[key_relation(a.camelot, b.camelot)]
    d = tempo_diff_pct(a.bpm, b.bpm)
    if d is None:
        cost += 1.0
    else:
        cost += d / 3
        if d > 8:
            cost += 3.0
    if direction and a.bpm and b.bpm:
        delta = b.bpm - a.bpm
        if delta * direction < -1:  # moving against the curve by more than 1 BPM
            cost += 1.5 + abs(delta) / 4
    return cost


def _direction(curve: str, position: int, n: int) -> int:
    if curve == "build":
        return 1
    if curve == "cooldown":
        return -1
    if curve == "peak":
        return 1 if position < n // 2 else -1
    return 0


def total_cost(order: list[SetTrack], curve: str) -> float:
    n = len(order)
    return sum(transition_cost(order[i], order[i + 1], _direction(curve, i, n)) for i in range(n - 1))


def _greedy(tracks: list[SetTrack], start: SetTrack, curve: str) -> list[SetTrack]:
    order = [start]
    left = [t for t in tracks if t is not start]
    n = len(tracks)
    while left:
        cur = order[-1]
        d = _direction(curve, len(order) - 1, n)
        nxt = min(left, key=lambda t: transition_cost(cur, t, d))
        order.append(nxt)
        left.remove(nxt)
    return order


def _two_opt(order: list[SetTrack], curve: str, max_passes: int = 8) -> list[SetTrack]:
    best, best_cost = order, total_cost(order, curve)
    n = len(order)
    for _ in range(max_passes):
        improved = False
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                cand = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                c = total_cost(cand, curve)
                if c < best_cost - 1e-9:
                    best, best_cost, improved = cand, c, True
        if not improved:
            break
    return best


def sequence(tracks: list[SetTrack], curve: str = "build") -> list[SetTrack]:
    if curve not in CURVES:
        raise ValueError(f"curve must be one of {CURVES}")
    tracks = list(tracks)
    if len(tracks) < 3:
        return sorted(tracks, key=lambda t: t.bpm or 0, reverse=curve == "cooldown")
    with_bpm = [t for t in tracks if t.bpm]
    if curve in ("build", "peak") and with_bpm:
        starts = sorted(with_bpm, key=lambda t: t.bpm)[:3]
    elif curve == "cooldown" and with_bpm:
        starts = sorted(with_bpm, key=lambda t: -t.bpm)[:3]
    else:
        starts = tracks if len(tracks) <= 60 else tracks[:: max(1, len(tracks) // 60)]
    best = min((_greedy(tracks, s, curve) for s in starts), key=lambda o: total_cost(o, curve))
    return _two_opt(best, curve) if len(best) <= 150 else best


def describe_transition(a: SetTrack, b: SetTrack) -> dict:
    rel = key_relation(a.camelot, b.camelot)
    d = tempo_diff_pct(a.bpm, b.bpm)
    if rel in ("same", "compatible") and d is not None and d <= 3:
        grade = "smooth"
    elif rel in ("same", "compatible", "energy boost") and d is not None and d <= 6:
        grade = "workable"
    else:
        grade = "hard"
    bits = []
    if a.camelot and b.camelot:
        bits.append(f"{a.camelot}→{b.camelot} ({rel})")
    else:
        bits.append("key unknown")
    if a.bpm and b.bpm:
        signed = (b.bpm - a.bpm) / a.bpm * 100
        half_double = abs(signed) - (d or 0) > 1
        bits.append(f"{a.bpm:g}→{b.bpm:g} BPM" + (" (half/double time)" if half_double else f" ({signed:+.1f}%)"))
    return {"grade": grade, "note": ", ".join(bits)}


def build_set_list(tracks: list[SetTrack], curve: str = "build") -> dict:
    order = sequence(tracks, curve)
    out = []
    for i, t in enumerate(order):
        row = {"position": i + 1, "id": t.id, "title": t.title, "artist": t.artist, "bpm": t.bpm, "camelot": t.camelot}
        if t.extra:
            row.update(dict(t.extra))
        if i:
            row["transition"] = describe_transition(order[i - 1], t)
        out.append(row)
    grades = [r["transition"]["grade"] for r in out[1:]]
    return {
        "curve": curve,
        "track_count": len(out),
        "transitions": {g: grades.count(g) for g in ("smooth", "workable", "hard")},
        "tracks": out,
    }

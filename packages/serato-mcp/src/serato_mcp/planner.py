"""Harmonic mix planning: transition scoring, next-track suggestions and full set construction (beam search
over the library against an energy curve)."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .jsnum import js_round, js_str, parse_float, round_to
from .keys import camelot_string, key_compatibility, parse_key, shifted_key
from .models import Track
from .tags import FileAnalysis


@dataclass(eq=False)
class MixTrack:
    track: Track
    analysis: FileAnalysis | None = None
    energy: float | None = None
    energyEstimated: bool = False


def identity(t: Track) -> str:
    """Same recording regardless of file location: artist + title, falling back to the path."""

    def n(x: str) -> str:
        x = re.sub(r"\(?(320kbps|clean|dirty)\)?", "", x.lower())
        return re.sub(r"[^a-z0-9]+", "", x)

    return n(t.artist) + "|" + n(t.title) if t.artist and t.title else t.path


def dedupe(pool: list[MixTrack]) -> list[MixTrack]:
    """Drop duplicate copies, preferring files that exist, have cues, then the higher bitrate."""

    def rank(m: MixTrack) -> float:
        bitrate = parse_float(m.track.bitrate or "0")
        has_cues = bool(m.analysis and m.analysis.cues)
        return (
            (0 if m.track.missing else 100)
            + (10 if has_cues else 0)
            + ((bitrate if bitrate == bitrate else 0) or 0) / 1000
        )

    best: dict[str, MixTrack] = {}
    for m in pool:
        k = identity(m.track)
        cur = best.get(k)
        if cur is None or rank(m) > rank(cur):
            best[k] = m
    return [m for m in pool if best[identity(m.track)] is m]


def label(t: Track) -> str:
    return f"{t.artist + ' - ' if t.artist else ''}{t.title if t.title is not None else os.path.basename(t.path)}"


# ---- tempo -------------------------------------------------------------------------


@dataclass
class TempoMatch:
    score: float
    pitchChangePct: float  # % the incoming track must be pitched to match the outgoing one
    mode: str  # how the incoming track's tempo relates to the outgoing one: direct | half-time | double-time | unknown


def tempo_compatibility(a: float | None, b: float | None) -> TempoMatch:
    if not a or not b:
        return TempoMatch(0.4, 0, "unknown")
    options = [("direct", a, 1.0), ("half-time", a / 2, 0.8), ("double-time", a * 2, 0.8)]
    options = [(mode, target, penalty, (target - b) / b * 100) for mode, target, penalty in options]
    best = options[0]
    for o in options[1:]:
        if abs(o[3]) * (2 - o[2]) < abs(best[3]) * (2 - best[2]):
            best = o
    d = abs(best[3])
    if d <= 1:
        base = 1.0
    elif d <= 3:
        base = 1 - (d - 1) * 0.08
    elif d <= 6:
        base = 0.84 - (d - 3) * 0.15
    elif d <= 10:
        base = 0.39 - (d - 6) * 0.09
    else:
        base = 0
    return TempoMatch(max(0, base) * best[2], round_to(best[3], 2), best[0])


# ---- energy ---------------------------------------------------------------------------


def _norm_bpm(b: float) -> float:
    """Fold BPMs into a 70..140 window so a 174 DnB track compares as 87 half-time."""
    return b / 2 if b > 140 else b * 2 if b < 70 else b


def with_energy(tracks: list[Track], analyses: dict[str, FileAnalysis | None]) -> list[MixTrack]:
    """Energy 1..10: Mixed In Key value when present, otherwise estimated from BPM within the pool."""
    bpms = sorted(_norm_bpm(t.bpm) for t in tracks if t.bpm)
    lo = bpms[math.floor(len(bpms) * 0.1)] if bpms else 110
    hi = bpms[math.floor(len(bpms) * 0.9)] if bpms else 130
    out = []
    for track in tracks:
        analysis = analyses.get(track.path)
        known = track.energy if track.energy is not None else (analysis.mikEnergy if analysis else None)
        if known:
            out.append(MixTrack(track, analysis, known, False))
        elif not track.bpm:
            out.append(MixTrack(track, analysis, None, True))
        else:
            x = (_norm_bpm(track.bpm) - lo) / (hi - lo) if hi > lo else 0.5
            out.append(MixTrack(track, analysis, round_to(3 + min(1, max(0, x)) * 5, 1), True))
    return out


@dataclass
class Curve:
    description: str
    fn: Callable[[float], float]


def _peak(t: float) -> float:
    return 8 + math.sin(t * math.pi * 3) * 0.7 - (1.5 if 0.6 < t < 0.72 else 0)


def _journey(t: float) -> float:
    return 4 + 5 * math.sin((t / 0.7) * (math.pi / 2)) if t < 0.7 else 9 - 3 * ((t - 0.7) / 0.3)


CURVES: dict[str, Curve] = {
    "warmup": Curve("Opening set: starts low (3) and eases up to 6 without peaking", lambda t: 3 + 3 * t),
    "build": Curve("Steady climb from 4 to 9", lambda t: 4 + 5 * t),
    "peak": Curve("Peak-time: holds 7-9 with a short breather two-thirds in", _peak),
    "journey": Curve("Classic arc: 4 → peak 9 around 70% → land at 6", _journey),
    "steady": Curve("Flat energy, holds the starting level", lambda t: 6),
    "cooldown": Curve("Closing set: 8 down to 3", lambda t: 8 - 5 * t),
    "rollercoaster": Curve("Waves between 5 and 9", lambda t: 7 + 2 * math.sin(t * math.pi * 4 - math.pi / 2)),
}


# ---- mix points -------------------------------------------------------------------------


def bars_to_sec(bars: float, bpm: float | None) -> float:
    return bars * 4 * 60 / bpm if bpm else bars * 2


def mix_points(m: MixTrack) -> dict:
    a = m.analysis
    duration = (a.durationSec if a and a.durationSec is not None else None) or None
    if duration is None:
        duration = m.track.durationSec
    cues = sorted(a.cues if a else [], key=lambda c: c.positionMs)
    bpm = m.track.bpm
    first_beat = a.beatGrid[0].positionSec if a and a.beatGrid else None
    mix_in = cues[0].positionMs / 1000 if cues else (first_beat if first_beat is not None else 0)
    mix_out = duration - bars_to_sec(32, bpm) if duration else 0
    source = "cue points" if cues else "beatgrid" if first_beat is not None else "track length"
    if duration and len(cues) >= 2:
        # The last cue in the final 40% usually marks the outro.
        last = cues[-1].positionMs / 1000
        if last > duration * 0.6 and duration - last >= bars_to_sec(8, bpm):
            mix_out = last
    if duration and mix_out <= mix_in:
        mix_out = max(mix_in + 30, duration * 0.8)
        source += " (short track)"
    return {
        "mixInSec": round_to(mix_in, 1),
        "mixOutSec": round_to(mix_out, 1),
        "durationSec": round_to(duration, 1) if duration else None,
        "source": source,
    }


# ---- transitions -------------------------------------------------------------------------


def _signed(x: float) -> str:
    return f"{'+' if x > 0 else ''}{js_str(x)}"


def transition(a: MixTrack, b: MixTrack) -> dict:
    ka = parse_key(a.track.camelot or a.track.key or (a.analysis.mikKey if a.analysis else None))
    kb = parse_key(b.track.camelot or b.track.key or (b.analysis.mikKey if b.analysis else None))
    key = key_compatibility(ka, kb)
    tempo = tempo_compatibility(a.track.bpm, b.track.bpm)
    de = round_to(b.energy - a.energy, 1) if a.energy is not None and b.energy is not None else None
    energy_score = 0.6 if de is None else max(0, 1 - max(0, abs(de) - 1) / 4)
    score = js_round((key.score * 0.45 + tempo.score * 0.4 + energy_score * 0.15) * 100)

    notes: list[str] = []
    d = abs(tempo.pitchChangePct)
    if tempo.mode == "unknown":
        technique, overlap = "Check BPM by ear: one of the tracks has no BPM analysed", 8
    elif tempo.score == 0:
        technique = (
            "Echo-out / cut: tempos are too far apart to beatmatch. Echo or reverb out the last phrase and drop the "
            "next track on its first downbeat"
        )
        overlap = 1
    elif tempo.mode != "direct":
        technique = (
            f"{'Half' if tempo.mode == 'half-time' else 'Double'}-time switch: bring the next track in over a "
            "breakdown so the groove change lands on a new phrase"
        )
        overlap = 8
    elif key.score >= 0.85 and d <= 3:
        technique, overlap = "Long blend: overlap 16-32 bars and swap the low EQ on a phrase change", 32
    elif key.score >= 0.55 and d <= 6:
        technique, overlap = "Standard blend: 16 bars with a bass swap at the drop", 16
    elif key.score < 0.55 and d <= 6:
        technique = (
            "Short blend or filter transition: keys clash, so keep the overlap to drums only (4-8 bars) and "
            "high-pass the outgoing track"
        )
        overlap = 4
    else:
        technique, overlap = "Tempo ride: loop the outro and pitch towards the next track's tempo before blending", 8
    if d > 3 and tempo.mode == "direct" and kb:
        landed = shifted_key(kb, tempo.pitchChangePct)
        if camelot_string(landed) != camelot_string(kb):
            notes.append(
                f"Turn key lock on: pitching {_signed(tempo.pitchChangePct)}% without it moves the key from "
                f"{camelot_string(kb)} to {camelot_string(landed)}"
            )
    if de is not None and de >= 3:
        notes.append(f"Big energy jump (+{js_str(de)}): drop it after a breakdown or build-up for impact")
    if de is not None and de <= -3:
        notes.append(f"Energy drops {js_str(de)}: mix out during the outgoing track's breakdown to avoid a sag")
    if a.energyEstimated or b.energyEstimated:
        notes.append("Energy is estimated from BPM for at least one track")

    pa, pb = mix_points(a), mix_points(b)
    return {
        "from": label(a.track),
        "to": label(b.track),
        "score": score,
        "key": {
            "from": camelot_string(ka) if ka else None,
            "to": camelot_string(kb) if kb else None,
            "relation": key.relation,
            "description": key.description,
        },
        "tempo": {"from": a.track.bpm, "to": b.track.bpm, "pitchChangePct": tempo.pitchChangePct, "mode": tempo.mode},
        "energy": {"from": a.energy, "to": b.energy, "delta": de},
        "technique": technique,
        "overlapBars": overlap,
        "mixOutAt": mmss(pa["mixOutSec"]) if pa["durationSec"] else None,
        "mixInAt": mmss(pb["mixInSec"]),
        "notes": notes,
    }


# ---- next-track suggestions ----------------------------------------------------------------


def suggest_next(
    current: MixTrack, pool: list[MixTrack], limit: int = 10, energy: str | None = None, exclude: set[str] | None = None
) -> list[dict]:
    out = []
    me = identity(current.track)
    exclude = exclude or set()
    for c in dedupe(pool):
        if identity(c.track) == me or c.track.path in exclude or identity(c.track) in exclude:
            continue
        tr = transition(current, c)
        s: float = tr["score"]
        de = tr["energy"]["delta"]
        if de is not None and energy and energy != "any":
            if energy == "up":
                s += 10 if 0 < de <= 2 else -15 if de <= 0 else 0
            if energy == "down":
                s += 10 if -2 <= de < 0 else -15 if de >= 0 else 0
            if energy == "same":
                s -= abs(de) * 5
        if same_artist(current.track, c.track):
            s -= 8
        out.append({"track": c.track, "score": js_round(s), "transition": tr})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---- full set planning -------------------------------------------------------------------------


@dataclass
class PlanOptions:
    count: int | None = None
    durationMin: float | None = None
    curve: str | None = None
    customCurve: list[float] | None = None  # energy targets spread evenly over the set
    startBpm: float | None = None
    endBpm: float | None = None
    start: MixTrack | None = None
    end: MixTrack | None = None
    mustInclude: list[MixTrack] = field(default_factory=list)
    harmonic: str | None = None  # strict | balanced | loose
    maxBpmJumpPct: float | None = None
    artistGap: int | None = None
    beamWidth: int | None = None


def _curve_at(opts: PlanOptions, t: float) -> float:
    if opts.customCurve:
        pts = opts.customCurve
        if len(pts) == 1:
            return pts[0]
        x = t * (len(pts) - 1)
        i = min(len(pts) - 2, math.floor(x))
        return pts[i] + (pts[i + 1] - pts[i]) * (x - i)
    return CURVES[opts.curve or "journey"].fn(t)


def _play_length(m: MixTrack) -> float:
    p = mix_points(m)
    return max(60, p["mixOutSec"] - p["mixInSec"]) if p["durationSec"] else 300


def _parse_clock(s: str) -> int:
    total = 0
    for n in s.split(":"):
        total = total * 60 + int(n)
    return total


def plan_mix(pool: list[MixTrack], opts: PlanOptions) -> dict:
    """Plan, then nudge the track count until the set lands close to duration_min."""
    plan = _plan_once(pool, opts)
    if not opts.durationMin or opts.count:
        return plan
    target = opts.durationMin * 60
    best = plan
    for _ in range(4):
        length = _parse_clock(plan["summary"]["estimatedLength"])
        avg = length / plan["summary"]["tracks"]
        diff = target - length
        if abs(diff) <= avg * 0.6:
            break
        count = max(2, plan["summary"]["tracks"] + js_round(diff / avg))
        if count == plan["summary"]["tracks"]:
            break
        plan = _plan_once(pool, PlanOptions(**{**opts.__dict__, "count": count}))
        if abs(_parse_clock(plan["summary"]["estimatedLength"]) - target) < abs(
            _parse_clock(best["summary"]["estimatedLength"]) - target
        ):
            best = plan
    return best


@dataclass
class _Beam:
    seq: list[MixTrack]
    used: set[str]
    score: float
    included: int


def _plan_once(pool: list[MixTrack], opts: PlanOptions) -> dict:
    warnings: list[str] = []
    forced = [m for m in [opts.start, opts.end, *opts.mustInclude] if m]
    forced_ids = {identity(m.track) for m in forced}
    first_forced = []
    for i, m in enumerate(forced):
        if next(j for j, x in enumerate(forced) if identity(x.track) == identity(m.track)) == i and m.track.bpm:
            first_forced.append(m)
    candidates = first_forced + dedupe([m for m in pool if m.track.bpm and identity(m.track) not in forced_ids])
    if len(pool) != len(candidates):
        warnings.append(f"{len(pool) - len(candidates)} tracks without BPM were left out")
    if len(candidates) < 2:
        raise ValueError("Need at least 2 analysed tracks (with BPM) to plan a mix")

    count = opts.count
    if not count:
        avg = sum(_play_length(m) for m in candidates) / len(candidates)
        count = max(2, js_round(opts.durationMin * 60 / avg)) if opts.durationMin else min(12, len(candidates))
    if count > len(candidates):
        warnings.append(f"Only {len(candidates)} usable tracks, so the set has {len(candidates)} instead of {count}")
        count = len(candidates)

    w_key = {"strict": 55, "balanced": 38, "loose": 20}[opts.harmonic or "balanced"]
    max_jump = opts.maxBpmJumpPct if opts.maxBpmJumpPct is not None else 8
    artist_gap = opts.artistGap if opts.artistGap is not None else 3
    must = [identity(m.track) for m in opts.mustInclude]
    must_set = set(must)
    width = opts.beamWidth or 48

    def bpm_target(t: float) -> float | None:
        if opts.startBpm and opts.endBpm:
            return opts.startBpm + (opts.endBpm - opts.startBpm) * t
        return opts.startBpm if opts.startBpm is not None else opts.endBpm

    def step_score(prev: MixTrack | None, c: MixTrack, i: int, seq: list[MixTrack]) -> float:
        t = 0 if count == 1 else i / (count - 1)
        target = _curve_at(opts, t)
        s = 0.0
        if c.energy is not None:
            s += (1 - min(1, abs(c.energy - target) / 5)) * 25
        else:
            s += 10
        bt = bpm_target(t)
        if bt and c.track.bpm:
            s += tempo_compatibility(bt, c.track.bpm).score * 20
        if identity(c.track) in must_set:
            s += 30
        if not prev:
            return s
        ka = parse_key(prev.track.camelot or prev.track.key)
        kb = parse_key(c.track.camelot or c.track.key)
        s += key_compatibility(ka, kb).score * w_key
        tempo = tempo_compatibility(prev.track.bpm, c.track.bpm)
        if abs(tempo.pitchChangePct) > max_jump:
            s -= 60
        s += tempo.score * 32
        if prev.energy is not None and c.energy is not None:
            jump = abs(c.energy - prev.energy)
            if jump > 2:
                s -= (jump - 2) * 6
        for k in range(max(0, len(seq) - artist_gap), len(seq)):
            if same_artist(seq[k].track, c.track):
                s -= 18
        if prev.track.genre and prev.track.genre == c.track.genre:
            s += 4
        return s

    end_id = identity(opts.end.track) if opts.end else None
    start_pool = [opts.start] if opts.start else candidates
    beam = [
        _Beam([m], {identity(m.track)}, step_score(None, m, 0, []), 1 if identity(m.track) in must_set else 0)
        for m in start_pool
        if not opts.end or identity(m.track) != end_id or count == 1
    ]
    beam = sorted(beam, key=lambda b: -b.score)[:width]

    for i in range(1, count):
        is_last = i == count - 1
        remaining = count - i
        nxt: list[_Beam] = []
        for st in beam:
            prev = st.seq[-1]
            missing_must = sum(1 for p in must_set if p not in st.used)
            if is_last and opts.end:
                options = [opts.end]
            else:
                options = [
                    m
                    for m in candidates
                    if identity(m.track) not in st.used and (not opts.end or identity(m.track) != end_id)
                ]
            # Reserve the remaining slots for required tracks once we run out of room.
            if not (is_last and opts.end) and missing_must >= remaining - (1 if opts.end else 0):
                required = [m for m in options if identity(m.track) in must_set]
                if required:
                    options = required
            scored = sorted(((c, step_score(prev, c, i, st.seq)) for c in options), key=lambda x: -x[1])[:14]
            for c, s in scored:
                cid = identity(c.track)
                nxt.append(
                    _Beam([*st.seq, c], st.used | {cid}, st.score + s, st.included + (1 if cid in must_set else 0))
                )
        if not nxt:
            break
        # Keep diverse beams: dedupe states ending on the same track with the same set.
        seen: set[str] = set()
        kept = []
        for s in sorted(nxt, key=lambda b: -b.score):
            sig = s.seq[-1].track.path + "|" + ",".join(sorted(s.used))
            if sig in seen:
                continue
            seen.add(sig)
            kept.append(s)
        beam = kept[:width]

    best = sorted(beam, key=lambda b: (-b.included, -b.score))[0]
    seq = best.seq
    missing = [m for m in opts.mustInclude if identity(m.track) not in best.used]
    if missing:
        warnings.append(f"Could not fit: {', '.join(label(m.track) for m in missing)}")

    transitions = [transition(seq[i], m) for i, m in enumerate(seq[1:])]
    tracks = []
    clock = 0.0
    # The next track starts when the outgoing one reaches its mix-out point (the start of its outro); the blend
    # runs over that outro.
    for i, m in enumerate(seq):
        p = mix_points(m)
        t = 0 if len(seq) == 1 else i / (len(seq) - 1)
        last = i == len(seq) - 1
        end_at = (p["durationSec"] if p["durationSec"] is not None else p["mixOutSec"]) if last else p["mixOutSec"]
        tracks.append(
            {
                "position": i + 1,
                "track": m.track,
                "energy": m.energy,
                "energyEstimated": m.energyEstimated,
                "targetEnergy": round_to(_curve_at(opts, t), 1),
                "startsAt": mmss(clock),
                "playFrom": mmss(p["mixInSec"]),
                "mixOutAt": mmss(end_at),
            }
        )
        clock += max(30, end_at - p["mixInSec"])

    scores = [t["score"] for t in transitions]
    avg_score = js_round(sum(scores) / len(scores)) if scores else 100
    weakest = scores.index(min(scores)) if scores else -1
    for i, t in enumerate(transitions):
        if t["key"]["relation"] == "clash":
            warnings.append(
                f"Key clash at transition {i + 1} → {i + 2} ({t['key']['from']} → {t['key']['to']}): "
                f"{t['technique'].split(':')[0]}"
            )
        if abs(t["tempo"]["pitchChangePct"]) > max_jump and t["tempo"]["mode"] == "direct":
            warnings.append(
                f"Large tempo jump at transition {i + 1} → {i + 2} ({js_str(t['tempo']['from'])} → "
                f"{js_str(t['tempo']['to'])} BPM)"
            )
    bpms = [m.track.bpm for m in seq if m.track.bpm]
    curve = (
        f"custom [{', '.join(js_str(x) for x in opts.customCurve)}]"
        if opts.customCurve
        else f"{opts.curve or 'journey'}: {CURVES[opts.curve or 'journey'].description}"
    )
    return {
        "summary": {
            "tracks": len(seq),
            "estimatedLength": mmss(clock),
            "averageTransitionScore": avg_score,
            "weakestTransition": {
                "position": weakest + 1,
                "score": scores[weakest],
                "between": f"{transitions[weakest]['from']} → {transitions[weakest]['to']}",
            }
            if weakest >= 0
            else None,
            "curve": curve,
            "bpmRange": [min(bpms), max(bpms)] if bpms else NULL,
            "keyPath": " → ".join(m.track.camelot or "?" for m in seq),
            "energyPath": " → ".join(
                "?" if m.energy is None else f"{js_str(m.energy)}{'~' if m.energyEstimated else ''}" for m in seq
            ),
        },
        "tracks": tracks,
        "transitions": transitions,
        "warnings": warnings,
    }


def analyze_set(seq: list[MixTrack]) -> dict:
    """Score an existing ordering."""
    transitions = [transition(seq[i], m) for i, m in enumerate(seq[1:])]
    avg = js_round(sum(t["score"] for t in transitions) / len(transitions)) if transitions else 100
    return {"averageTransitionScore": avg, "transitions": transitions}


def reorder_set(
    seq: list[MixTrack], keep_first: bool = False, keep_last: bool = False, curve: str | None = None
) -> dict:
    """Suggest the best re-ordering of the same tracks."""
    return plan_mix(
        seq,
        PlanOptions(
            count=len(seq),
            start=seq[0] if keep_first else None,
            end=seq[-1] if keep_last else None,
            mustInclude=list(seq),
            curve=curve or "steady",
            beamWidth=96,
        ),
    )


# ---- helpers ------------------------------------------------------------------------------


def _artists(t: Track) -> list[str]:
    parts = re.split(r",|&| x | feat\.? | ft\.? | vs\.? ", (t.artist or "").lower())
    return [s.strip() for s in parts if s.strip()]


def same_artist(a: Track, b: Track) -> bool:
    x = _artists(a)
    return bool(x) and any(n in x for n in _artists(b))


def mmss(sec: float) -> str:
    s = max(0, js_round(sec))
    h, m, r = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{r:02d}" if h else f"{m}:{r:02d}"


class _Null:
    """An explicit JSON null (plain None means "leave the key out", like undefined in JavaScript)."""

    def __repr__(self) -> str:
        return "null"


NULL = _Null()

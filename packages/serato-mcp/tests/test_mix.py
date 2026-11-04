from dataclasses import replace

import pytest

from serato_mcp.jsnum import iso, js_round, round_to, to_fixed
from serato_mcp.keys import camelot_string, compatible_keys, key_compatibility, parse_key, shifted_key
from serato_mcp.models import Track
from serato_mcp.planner import MixTrack, PlanOptions, mmss, plan_mix, tempo_compatibility, transition


@pytest.mark.parametrize(
    "text, want",
    [("Am", "8A"), ("A minor", "8A"), ("Amin", "8A"), ("C", "8B"), ("Cmaj", "8B"), ("F#m", "11A"), ("Gbmin", "11A"),
     ("D#min", "2A"), ("Ebm", "2A"), ("Bb", "6B"), ("Abmaj", "4B"), ("Bm", "10A"), ("C#m", "12A"), ("8A", "8A"),
     ("12b", "12B"), ("1m", "8A"), ("6d", "1B")],
)  # fmt: skip
def test_key_parsing(text, want):
    assert camelot_string(parse_key(text)) == want


def test_bad_keys():
    assert parse_key("H#") is None
    assert parse_key("13A") is None


def test_harmonic_relations():
    def rel(a: str, b: str) -> str:
        return key_compatibility(parse_key(a), parse_key(b)).relation

    assert rel("8A", "8A") == "same"
    assert rel("8A", "9A") == "+1"
    assert rel("8A", "7A") == "-1"
    assert rel("8A", "8B") == "relative"
    assert rel("12A", "1A") == "+1"
    assert rel("8A", "3A") == "+7"
    assert rel("8A", "9B") == "diagonal"
    assert rel("8A", "2B") == "clash"
    assert any(k["key"] == "8B" for k in compatible_keys(parse_key("8A")))
    assert camelot_string(shifted_key(parse_key("8A"), 6)) == "3A"  # +1 semitone


def test_tempo_matching_handles_half_and_double_time():
    assert tempo_compatibility(126, 126).score == 1
    assert tempo_compatibility(87, 174).mode == "double-time"  # incoming relative to outgoing
    assert tempo_compatibility(174, 87).mode == "half-time"
    assert tempo_compatibility(100, 140).score == 0
    assert abs(tempo_compatibility(128, 126).pitchChangePct - 1.59) < 0.01


def test_javascript_compatible_numbers():
    assert js_round(2.5) == 3 and js_round(-2.5) == -2  # Math.round
    assert round_to(1.005, 2) == 1  # 1.005 is really 1.00499...
    assert to_fixed(0.125, 2) == "0.13" and to_fixed(1.5, 0) == "2"
    assert iso(1700000000) == "2023-11-14T22:13:20.000Z"
    assert mmss(59.5) == "1:00" and mmss(3725) == "1:02:05"


def fake(n: int, bpm: float, key: str, energy: float, artist: str | None = None) -> MixTrack:
    k = parse_key(key)
    t = Track(
        path=f"/t/{n}.mp3", storedPath=f"t/{n}.mp3", root="/", title=f"T{n}", artist=artist or f"A{n}",
        bpm=bpm, key=key, camelot=camelot_string(k), durationSec=360,
    )  # fmt: skip
    return MixTrack(t, None, energy, False)


def test_planner_unique_tracks_pins_and_curve():
    pool = [
        fake(1, 122, "8A", 3), fake(2, 123, "9A", 4), fake(3, 124, "9A", 5), fake(4, 125, "10A", 6),
        fake(5, 126, "10A", 7), fake(6, 127, "11A", 8), fake(7, 128, "11A", 9), fake(8, 126, "3B", 6),
        fake(9, 140, "5A", 5), fake(10, 124, "8B", 4),
    ]  # fmt: skip
    plan = plan_mix(pool, PlanOptions(count=6, curve="build", start=pool[0], end=pool[6], mustInclude=[pool[3]]))
    paths = [t["track"].path for t in plan["tracks"]]
    assert len(set(paths)) == len(paths)
    assert paths[0] == "/t/1.mp3" and paths[-1] == "/t/7.mp3"
    assert "/t/4.mp3" in paths
    assert "/t/9.mp3" not in paths, "140 BPM outlier is avoided"
    energies = [t["energy"] for t in plan["tracks"]]
    assert energies[-1] > energies[0], "energy builds"
    assert plan["summary"]["averageTransitionScore"] >= 80


def test_planner_never_uses_two_copies_of_a_track():
    a = fake(1, 126, "8A", 5, "Same")
    b = fake(2, 126, "8A", 5, "Same")
    b.track = replace(b.track, title="T1")
    plan = plan_mix([a, b, fake(3, 126, "9A", 6), fake(4, 126, "8A", 6)], PlanOptions(count=3))
    ids = [f"{t['track'].artist}|{t['track'].title}" for t in plan["tracks"]]
    assert len(set(ids)) == len(ids)


def test_transition_advice():
    assert "Long blend" in transition(fake(1, 126, "8A", 5), fake(2, 126, "8A", 6))["technique"]
    assert transition(fake(1, 126, "8A", 5), fake(2, 126, "2B", 6))["key"]["relation"] == "clash"
    assert "Echo-out" in transition(fake(1, 100, "8A", 5), fake(2, 140, "8A", 6))["technique"]

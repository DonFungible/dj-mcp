import pytest

from rekordbox_mcp.sequencing import (
    SetTrack,
    build_set_list,
    describe_transition,
    key_relation,
    sequence,
    tempo_diff_pct,
    total_cost,
)


def t(i, bpm, key):
    return SetTrack(str(i), f"T{i}", "A", bpm, key)


def test_tempo_and_key_helpers():
    assert tempo_diff_pct(128, 128) == 0
    assert tempo_diff_pct(140, 70) == 0
    assert tempo_diff_pct(None, 128) is None
    assert key_relation("8A", "8A") == "same"
    assert key_relation("8A", "9A") == "compatible"
    assert key_relation("8A", "8B") == "compatible"
    assert key_relation("8A", "10A") == "energy boost"
    assert key_relation("8A", "2B") == "clash"
    assert key_relation(None, "2B") == "unknown"


def test_build_rises_and_cooldown_falls():
    tracks = [t(1, 128, "8A"), t(2, 122, "7A"), t(3, 125, "8A"), t(4, 120, "6A"), t(5, 126, "9A"), t(6, 124, "8B")]
    up = [x.bpm for x in sequence(tracks, "build")]
    down = [x.bpm for x in sequence(tracks, "cooldown")]
    # Dips of up to 1 BPM are allowed when they avoid a key clash (inaudible once tempo-matched).
    assert all(b - a >= -1 for a, b in zip(up, up[1:], strict=False)) and up[-1] > up[0]
    assert all(a - b >= -1 for a, b in zip(down, down[1:], strict=False)) and down[-1] < down[0]


def test_peak_goes_up_then_down():
    tracks = [t(i, bpm, "8A") for i, bpm in enumerate([118, 120, 122, 124, 126, 128, 130, 121, 125])]
    bpms = [x.bpm for x in sequence(tracks, "peak")]
    top = bpms.index(max(bpms))
    assert 0 < top < len(bpms) - 1


def test_smooth_beats_naive_order():
    tracks = [t(1, 128, "8A"), t(2, 100, "3B"), t(3, 127, "9A"), t(4, 101, "3B"), t(5, 129, "8B"), t(6, 99, "4B")]
    best = sequence(tracks, "smooth")
    assert total_cost(best, "smooth") < total_cost(tracks, "smooth")
    assert sorted(x.id for x in best) == sorted(x.id for x in tracks)  # nothing lost or duplicated


def test_set_list_shape_and_grades():
    tracks = [t(1, 124, "8A"), t(2, 125, "9A"), t(3, 140, "2B")]
    res = build_set_list(tracks, "build")
    assert res["track_count"] == 3 and "transition" not in res["tracks"][0]
    assert sum(res["transitions"].values()) == 2
    assert describe_transition(tracks[0], tracks[1])["grade"] == "smooth"
    assert describe_transition(tracks[1], tracks[2])["grade"] == "hard"
    assert "half/double" in describe_transition(t(1, 140, "8A"), t(2, 70, "8A"))["note"]


def test_extra_passthrough_and_bad_curve():
    res = build_set_list([SetTrack("1", "x", "y", 120, "8A", (("spotify_uri", "spotify:track:1"),))], "build")
    assert res["tracks"][0]["spotify_uri"] == "spotify:track:1"
    with pytest.raises(ValueError):
        sequence([], "zigzag")

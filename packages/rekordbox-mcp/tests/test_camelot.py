import pytest

from rekordbox_mcp.camelot import bpm_compatible, compatible_camelot, key_name, to_camelot


@pytest.mark.parametrize(
    "key,expected",
    [
        ("Am", "8A"),
        ("C", "8B"),
        ("G#m", "1A"),
        ("Abm", "1A"),
        ("F#", "2B"),
        ("Gb", "2B"),
        ("8a", "8A"),
        ("12B", "12B"),
        ("Dbm", "12A"),
        ("C#m", "12A"),
        ("bm", "10A"),
        ("Bb", "6B"),
        ("E minor", "9A"),
        (None, None),
        ("", None),
        ("nonsense", None),
        ("13A", None),
    ],
)
def test_to_camelot(key, expected):
    assert to_camelot(key) == expected


def test_round_trip():
    for n in range(1, 13):
        for letter in "AB":
            code = f"{n}{letter}"
            assert to_camelot(key_name(code)) == code


def test_compatible_wraps_around():
    assert compatible_camelot("12A") == ["12A", "1A", "11A", "12B"]
    assert compatible_camelot("1B") == ["1B", "2B", "12B", "1A"]
    assert set(compatible_camelot("8A", energy_boost=True)) == {"8A", "9A", "7A", "8B", "10A", "3A"}


def test_bpm_compatible():
    assert bpm_compatible(128, 130, 2)
    assert not bpm_compatible(128, 135, 2)
    assert bpm_compatible(140, 70, 1)
    assert not bpm_compatible(140, 70, 1, allow_double_time=False)
    assert not bpm_compatible(0, 128, 5)

"""Read-only checks against the real local rekordbox library. Skipped if none is found."""

import pytest

from rekordbox_mcp.library import RekordboxLibrary, WriteNotAllowed


@pytest.fixture(scope="module")
def lib():
    lib = RekordboxLibrary(allow_write=False)
    try:
        _ = lib.db
    except Exception as e:  # noqa: BLE001 - no rekordbox install / key unavailable
        pytest.skip(f"rekordbox library unavailable: {e}")
    yield lib
    lib.close()


def test_stats(lib):
    s = lib.stats()
    assert s["track_count"] >= 0
    assert s["writes_enabled"] is False


def test_search_and_detail(lib):
    res = lib.search_tracks(limit=5)
    assert res["returned"] <= 5
    if not res["tracks"]:
        pytest.skip("empty library")
    t = lib.get_track(res["tracks"][0]["id"])
    assert t["id"] == res["tracks"][0]["id"]
    assert isinstance(t["cues"], list)


def test_bpm_filter(lib):
    for t in lib.search_tracks(bpm_min=120, bpm_max=130, limit=1000)["tracks"]:
        assert 120 <= t["bpm"] <= 130


def test_playlists_and_history(lib):
    tree = lib.list_playlists()
    for node in tree:
        if node["type"] == "playlist":
            pl = lib.get_playlist_tracks(node["id"])
            assert pl["total"] == node["track_count"]
            break
    hist = lib.list_history(limit=1)
    if hist:
        lib.get_history_tracks(hist[0]["id"])
    lib.list_my_tags()


def test_compatible(lib):
    tracks = [t for t in lib.search_tracks(limit=1000)["tracks"] if t["camelot"] and t["bpm"]]
    if not tracks:
        pytest.skip("no analysed tracks")
    res = lib.find_compatible_tracks(tracks[0]["id"])
    for t in res["tracks"]:
        assert t["camelot"] in res["compatible_keys"]


def test_writes_blocked_by_default(lib):
    with pytest.raises(WriteNotAllowed):
        lib.create_playlist("should-not-exist")


def test_bad_ids(lib):
    with pytest.raises(ValueError):
        lib.get_track("does-not-exist")
    with pytest.raises(ValueError):
        lib.search_tracks(key="H#")

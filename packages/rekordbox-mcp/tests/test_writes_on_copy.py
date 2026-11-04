"""Write-path tests. They ONLY ever run against a temporary copy of the library."""

import shutil
from pathlib import Path

import pytest

from rekordbox_mcp.library import RekordboxLibrary

FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    try:
        real = RekordboxLibrary(allow_write=False)
        real_dir = Path(real.db.db_directory)
        real.close()
    except Exception as e:  # noqa: BLE001 - no rekordbox install on this machine
        pytest.skip(f"no local rekordbox library to copy: {e}")
    for f in FILES:
        if (real_dir / f).exists():
            shutil.copy2(real_dir / f, tmp_path / f)
    import pyrekordbox.masterdb.database as dbm

    monkeypatch.setattr(dbm, "get_rekordbox_pid", lambda: 0)
    monkeypatch.setattr(RekordboxLibrary, "rekordbox_running", staticmethod(lambda: False))
    lib = RekordboxLibrary(db_dir=str(tmp_path), allow_write=True)
    # Hard safety stop: never proceed unless the copy is what got opened.
    assert Path(lib.db.engine.url.database).resolve() == (tmp_path / "master.db").resolve()
    yield lib
    lib.close()


def test_playlist_lifecycle(lib, tmp_path):
    ids = [t["id"] for t in lib.search_tracks(limit=3)["tracks"]]
    if len(ids) < 3:
        pytest.skip("need at least 3 tracks")
    folder = lib.create_playlist_folder("pytest folder")
    pl = lib.create_playlist("pytest playlist", parent_id=folder["id"], track_ids=ids[:2])

    res = lib.add_tracks_to_playlist(pl["id"], ids)
    assert res["added"] == [ids[2]] and sorted(res["skipped_already_present"]) == sorted(ids[:2])
    assert [t["id"] for t in lib.get_playlist_tracks(pl["id"])["tracks"]] == ids

    assert lib.remove_tracks_from_playlist(pl["id"], [ids[0]])["removed"] == [ids[0]]
    lib.rename_playlist(pl["id"], "renamed")

    lib.close()  # reopen to prove the changes were committed
    node = next(n for n in lib.list_playlists() if n["id"] == folder["id"])
    assert node["children"][0]["name"] == "renamed"
    assert node["children"][0]["track_count"] == 2

    lib.delete_playlist(folder["id"])
    assert folder["id"] not in {n["id"] for n in lib.list_playlists()}
    assert list((tmp_path / "rekordbox-mcp-backups").iterdir())


def test_refuses_while_rekordbox_running(lib, monkeypatch):
    from rekordbox_mcp.library import WriteNotAllowed

    monkeypatch.setattr(RekordboxLibrary, "rekordbox_running", staticmethod(lambda: True))
    with pytest.raises(WriteNotAllowed, match="running"):
        lib.create_playlist("nope")

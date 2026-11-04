"""The MCP tools against a small Serato 3 style library built from scratch (no Serato needed)."""

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from serato_mcp import server, writer
from serato_mcp.binary import Field, decode, decode_utf16be, encode, get_child, make_field
from serato_mcp.library import Library
from serato_mcp.models import SeratoRoot

TRACKS = [
    ("Artist One", "Opener", "124.00", "Am"),
    ("Artist Two", "Builder", "125.00", "Em"),
    ("Artist Three", "Peak", "126.00", "Bm"),
]


@pytest.fixture
def library(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    root = tmp_path / "_Serato_"
    (root / "Subcrates").mkdir(parents=True)
    fields = [make_field("vrsn", "2.0/Serato Scratch LIVE Database")]
    for i, (artist, title, bpm, key) in enumerate(TRACKS):
        audio = music / f"{i}.mp3"
        audio.write_bytes(b"")
        stored = str(audio).lstrip("/")
        fields.append(
            Field(
                "otrk",
                b"",
                [
                    make_field("ttyp", "mp3"),
                    make_field("pfil", stored),
                    make_field("tsng", title),
                    make_field("tart", artist),
                    make_field("tbpm", bpm),
                    make_field("tkey", key),
                    make_field("tlen", "05:00.00"),
                    make_field("tcom", f"Energy {5 + i}"),
                ],
            )  # fmt: skip
        )
    (root / "database V2").write_bytes(encode(fields))
    monkeypatch.setenv("SERATO_BACKEND", "legacy")
    monkeypatch.setenv("SERATO_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("SERATO_MCP_READONLY", raising=False)
    monkeypatch.setattr(writer, "serato_running", lambda: False)
    monkeypatch.setattr(server, "serato_running", lambda: False)
    monkeypatch.setattr(server, "lib", Library([SeratoRoot(str(root), "/")]))
    return root, [str(music / f"{i}.mp3") for i in range(len(TRACKS))]


def call(tool: str, /, **args):
    result = asyncio.run(server.mcp.call_tool(tool, args))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def call_error(tool: str, /, **args) -> str:
    with pytest.raises(ToolError) as err:
        asyncio.run(server.mcp.call_tool(tool, args))
    return str(err.value)


def test_tools_and_prompts_are_registered():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert len(tools) == 38
    assert {"plan_mix", "remove_from_crate", "compatible_keys", "analyze_set", "deck_control"} <= tools
    assert {p.name for p in asyncio.run(server.mcp.list_prompts())} == {"plan_set", "whats_next"}


def test_reads(library):
    _, paths = library
    status = call("serato_status")
    assert status["tracks"] == 3 and status["backend"].startswith("Legacy") and status["lastSession"] is None
    found = call("search_tracks", query="builder")
    assert [t["title"] for t in found["tracks"]] == ["Builder"]
    assert found["tracks"][0]["key"] == "9A (Em)" and found["tracks"][0]["energy"] == 6
    assert call("search_tracks", compatible_with_key="8A")["total"] == 3  # 8A, 9A (+1) and 10A (+2)
    t = call("get_track", track="artist three peak")
    assert t["path"] == paths[2] and t["camelot"] == "10A" and t["phrase"]["8 bars"] == "15.2s"
    tr = call("analyze_transition", **{"from": paths[0], "to": paths[1]})
    assert tr["key"]["relation"] == "+1" and tr["tempo"]["pitchChangePct"] == -0.8
    plan = call("plan_mix", count=3, curve="build")
    assert [s["title"] for s in plan["setlist"]] == ["Opener", "Builder", "Peak"]
    assert "Unrecognised key" in call_error("search_tracks", key="H#")


def test_crate_lifecycle(library):
    root, paths = library
    made = call("create_crate", name="Sets / Friday", tracks=[paths[0], "builder"])
    assert made["added"] == 2 and (root / "Subcrates" / "Sets%%Friday.crate").exists()
    assert (root / "Subcrates" / "Sets.crate").exists(), "parent crate is created for nesting"
    assert "already exists" in call_error("create_crate", name="Sets / Friday", tracks=[paths[1]])
    added = call("add_to_crate", name="Sets / Friday", tracks=[paths[2], paths[0]])
    assert added["added"] == 1 and added["skippedDuplicates"] == 1
    assert call("remove_from_crate", name="Friday", tracks=[paths[0]])["removed"] == 1
    crate = call("get_crate", name="Sets / Friday")
    assert [t["title"] for t in crate["tracks"]] == ["Builder", "Peak"]
    call("rename_crate", name="Sets", new_name="Gigs")
    assert {c["name"] for c in call("list_crates")} == {"Gigs", "Gigs / Friday"}
    deleted = call("delete_crate", name="Gigs")
    assert len(deleted["files"]) == 2 and not any((root / "Subcrates").iterdir())
    assert Path(deleted["backupDir"]).exists()
    assert "unsupported character" in call_error("create_crate", name="Bad:Name", tracks=[paths[0]])


def test_metadata_edits(library):
    root, paths = library
    res = call("update_tracks", edits=[{"track": paths[0], "comment": "edited", "color": "#00ff00", "bpm": "124.5"}])
    assert res["results"] == [{"path": paths[0], "updated": ["comment", "bpm", "color"]}]
    t = call("get_track", track=paths[0])
    assert (t["comment"], t["color"], t["bpm"]) == ("edited", "#00ff00", 124.5)
    otrk = next(f for f in decode((root / "database V2").read_bytes()) if f.tag == "otrk")
    assert decode_utf16be(get_child(otrk, "tbpm").raw) == "124.50"
    assert "bpm must be a number" in call_error("update_tracks", edits=[{"track": paths[0], "bpm": "fast"}])


def test_writes_are_refused_when_unsafe(library, monkeypatch):
    _, paths = library
    monkeypatch.setattr(writer, "serato_running", lambda: True)
    assert "Serato DJ is running" in call_error("create_crate", name="X", tracks=[paths[0]])
    assert call("create_crate", name="X", tracks=[paths[0]], force=True)["added"] == 1
    monkeypatch.setenv("SERATO_MCP_READONLY", "1")
    assert "read-only mode" in call_error("delete_crate", name="X", force=True)

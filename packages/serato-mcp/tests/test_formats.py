import base64
import os
import shutil
import struct
from pathlib import Path

import pytest

from serato_mcp.binary import Field, decode, decode_utf16be, encode, encode_utf16be, get_child, make_field
from serato_mcp.library import Library, crate_display_name, crate_file_name, parse_session_file
from serato_mcp.models import SeratoRoot
from serato_mcp.tags import BeatGridMarker, CuePoint, Loop, parse_autotags, parse_beatgrid, parse_markers2
from serato_mcp.writer import write_crate_tracks

SERATO = Path.home() / "Music" / "_Serato_"


@pytest.mark.skipif(not SERATO.exists(), reason="no Serato library on this machine")
def test_tlv_round_trips_byte_identically_on_real_files():
    files = [SERATO / "database V2", *sorted((SERATO / "Subcrates").glob("*.crate"))]
    for f in files:
        if f.exists():
            data = f.read_bytes()
            assert encode(decode(data)) == data, f


@pytest.mark.skipif(not (SERATO / "Subcrates").exists(), reason="no Serato crates on this machine")
def test_crates_we_write_match_crates_serato_wrote(tmp_path, monkeypatch):
    root = tmp_path / "_Serato_"
    shutil.copytree(SERATO / "Subcrates", root / "Subcrates")
    monkeypatch.setenv("SERATO_BACKEND", "legacy")
    monkeypatch.setenv("SERATO_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("SERATO_MCP_READONLY", raising=False)
    lib = Library([SeratoRoot(str(root), "/")]).refresh()

    def columns(b: bytes) -> list[str]:
        return [decode_utf16be(get_child(f, "tvcn").raw) for f in decode(b) if f.tag == "ovct"]

    def without_columns(b: bytes) -> bytes:
        return encode([f for f in decode(b) if f.tag != "ovct"])

    for c in list(lib.crates):
        before = Path(c.file).read_bytes()
        name = c.name + " copy"
        write_crate_tracks(lib, name, c.trackPaths, "create", force=True)
        after = (root / "Subcrates" / crate_file_name(name)).read_bytes()
        if columns(before) == columns(after):  # same columns Serato uses by default
            assert without_columns(before) == without_columns(after), c.name
        assert sum(1 for f in decode(after) if f.tag == "otrk") == len(c.trackPaths)


def test_utf16_and_field_helpers():
    assert decode_utf16be(encode_utf16be("Señor ♫ 日本")) == "Señor ♫ 日本"
    assert decode_utf16be(make_field("vrsn", "1.0/Serato ScratchLive Crate").raw) == "1.0/Serato ScratchLive Crate"
    assert struct.unpack(">I", make_field("ulbl", 0xFFFFFF).raw)[0] == 0xFFFFFF
    assert crate_file_name("Parent / Child") == "Parent%%Child.crate"
    assert crate_display_name("Parent%%Child.crate") == "Parent / Child"
    with pytest.raises(ValueError):
        crate_file_name("a:b")


def markers2(entries: list[tuple[str, bytes]]) -> bytes:
    """A Markers2 payload the way Serato writes it: 01 01 + base64(01 01 + entries) + NUL padding."""
    body = b"\x01\x01" + b"".join(name.encode() + b"\0" + struct.pack(">I", len(d)) + d for name, d in entries)
    b64 = base64.b64encode(body).decode().rstrip("=")
    wrapped = "\n".join(b64[i : i + 72] for i in range(0, len(b64), 72))
    return b"\x01\x01" + wrapped.encode() + b"\0" * 16


def test_markers2_cues_loops_colour_bpm_lock():
    cue = bytes([0, 2]) + bytes([0, 0, 0x30, 0x39]) + bytes([0, 0xCC, 0, 0, 0, 0]) + b"Drop\0"
    loop = (
        bytes([0, 0]) + bytes([0, 0, 0x03, 0xE8]) + bytes([0, 0, 0x07, 0xD0]) + b"\xff" * 4
        + bytes([0, 0x27, 0xAA, 0xE1]) + bytes([0, 1]) + b"Intro\0"
    )  # fmt: skip
    parsed = parse_markers2(
        markers2([("COLOR", bytes([0, 0xFF, 0x99, 0])), ("CUE", cue), ("LOOP", loop), ("BPMLOCK", b"\x01")])
    )
    assert parsed["cues"] == [CuePoint(2, 12345, "#cc0000", "Drop")]
    assert parsed["loops"] == [Loop(0, 1000, 2000, True, "Intro")]
    assert parsed["trackColor"] == "#ff9900"
    assert parsed["bpmLocked"] is True


def test_beatgrid_and_autotags():
    grid = b"\x01\x00" + struct.pack(">I", 2) + struct.pack(">fI", 0.5, 64) + struct.pack(">ff", 30.5, 125) + b"\0"
    assert parse_beatgrid(grid) == [BeatGridMarker(0.5, beatsUntilNext=64), BeatGridMarker(30.5, bpm=125)]
    assert parse_autotags(b"\x01\x01126.00\x00-1.500\x000.000\x00") == {"autoBpm": 126, "autoGainDb": 0}


def test_legacy_history_session_files(tmp_path):
    def adat(fields: list[tuple[int, bytes]]) -> bytes:
        return b"".join(struct.pack(">II", i, len(raw)) + raw for i, raw in fields)

    def text(s: str) -> bytes:
        return encode_utf16be(s + "\0")

    def entry(title: str, deck: int, start: int) -> bytes:
        a = adat(
            [(2, text(f"/Music/{title}.mp3")), (6, text(title)), (7, text("Artist")), (15, text("126")),
             (28, struct.pack(">I", start)), (31, struct.pack(">I", deck)), (50, b"\x01")]
        )  # fmt: skip
        return encode([Field("oent", b"", [Field("adat", a)])])

    f = tmp_path / "1.session"
    head = encode([make_field("vrsn", "1.0/Serato Scratch LIVE Review")])
    f.write_bytes(head + entry("One", 1, 1700000000) + entry("Two", 2, 1700000300))
    s = parse_session_file(str(f))
    assert len(s.entries) == 2
    assert s.entries[1].title == "Two" and s.entries[1].deck == 2
    assert s.entries[0].path == "/Music/One.mp3"
    assert s.startedAt == "2023-11-14T22:13:20.000Z"
    assert os.path.basename(s.file) == "1.session"

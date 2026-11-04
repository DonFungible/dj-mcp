"""Serato's tag-length-value container format, used by `database V2`, `*.crate` and `*.scrate` files.

Each field is a 4-byte ASCII tag, a 4-byte big-endian length, then the payload. The first letter of the
tag gives the payload type:
  o  nested fields        t  UTF-16BE text      p  UTF-16BE path
  u  uint32               s  uint16             b  single byte
  r  nested fields (smart-crate rules)
Anything else is kept as raw bytes.

Every field keeps its original bytes, so a file that is decoded and re-encoded without changes comes out
byte-identical.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

NESTED = {"o", "r"}
TEXT_TAGS = {"vrsn"}

FieldValue = str | int | bool | bytes


@dataclass
class Field:
    tag: str
    raw: bytes = b""
    children: list[Field] | None = None


def is_nested_tag(tag: str) -> bool:
    return tag[0] in NESTED


def decode(buf: bytes) -> list[Field]:
    fields: list[Field] = []
    pos = 0
    while pos + 8 <= len(buf):
        tag = buf[pos : pos + 4].decode("latin1")
        (length,) = struct.unpack_from(">I", buf, pos + 4)
        start = pos + 8
        end = start + length
        if end > len(buf):
            raise ValueError(f"Corrupt Serato data: field {tag} at {pos} overruns buffer")
        raw = bytes(buf[start:end])
        f = Field(tag, raw)
        if is_nested_tag(tag):
            f.children = decode(raw)
        fields.append(f)
        pos = end
    return fields


def encode(fields: list[Field]) -> bytes:
    parts: list[bytes] = []
    for f in fields:
        payload = encode(f.children) if f.children is not None else f.raw
        parts.append(f.tag.encode("latin1")[:4].ljust(4, b"\0") + struct.pack(">I", len(payload)))
        parts.append(payload)
    return b"".join(parts)


def decode_utf16be(raw: bytes) -> str:
    return raw[: len(raw) - len(raw) % 2].decode("utf-16-be", errors="replace")


def encode_utf16be(text: str) -> bytes:
    return text.encode("utf-16-be")


def read_value(f: Field) -> FieldValue:
    if f.tag in TEXT_TAGS:
        return decode_utf16be(f.raw)
    kind = f.tag[0]
    if kind in ("t", "p"):
        return decode_utf16be(f.raw)
    if kind == "u":
        return struct.unpack(">I", f.raw)[0] if len(f.raw) == 4 else f.raw
    if kind == "s":
        return struct.unpack(">H", f.raw)[0] if len(f.raw) == 2 else f.raw
    if kind == "b":
        return f.raw[0] != 0 if len(f.raw) == 1 else f.raw
    return f.raw


def make_field(tag: str, value: FieldValue) -> Field:
    if isinstance(value, bytes):
        return Field(tag, value)
    if tag in TEXT_TAGS or tag[0] in ("t", "p"):
        return Field(tag, encode_utf16be(str(value)))
    if tag[0] == "u":
        return Field(tag, struct.pack(">I", int(value) & 0xFFFFFFFF))
    if tag[0] == "s":
        return Field(tag, struct.pack(">H", int(value) & 0xFFFF))
    if tag[0] == "b":
        return Field(tag, bytes([1 if value else 0]))
    raise ValueError(f"Cannot encode value for tag {tag}")


def get_child(parent: Field, tag: str) -> Field | None:
    return next((c for c in parent.children or [] if c.tag == tag), None)


def set_child(parent: Field, tag: str, value: FieldValue) -> None:
    """Set (or insert) a child value, keeping its position when it already exists."""
    if parent.children is None:
        raise ValueError(f"{parent.tag} is not a nested field")
    new = make_field(tag, value)
    for i, c in enumerate(parent.children):
        if c.tag == tag:
            parent.children[i] = new
            return
    parent.children.append(new)


def remove_child(parent: Field, tag: str) -> None:
    if parent.children is not None:
        parent.children = [c for c in parent.children if c.tag != tag]

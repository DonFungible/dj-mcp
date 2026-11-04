"""Safe writes to Serato's library files.

Serato keeps its library in memory and rewrites `database V2` and crate files on exit, so edits made while
it runs are lost or can corrupt the library. Every write here therefore:
  1. refuses while Serato DJ is running (unless `force`),
  2. copies each file it is about to change into ~/.serato-mcp/backups/<stamp>/,
  3. writes atomically (temp file + rename).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from .binary import (
    Field,
    decode,
    decode_utf16be,
    encode,
    encode_utf16be,
    get_child,
    make_field,
    remove_child,
    set_child,
)
from .jsnum import to_fixed
from .library import Library, crate_file_name, is_streaming, root_for_path, to_stored
from .models import Crate, SeratoRoot


def serato_running() -> bool:
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"Serato DJ (Pro|Lite)\.app/Contents/MacOS/"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return False
    return bool(out.strip())


def assert_writable(force: bool | None = None) -> None:
    if os.environ.get("SERATO_MCP_READONLY") == "1":
        raise PermissionError("serato-mcp is in read-only mode (SERATO_MCP_READONLY=1)")
    if not force and serato_running():
        raise PermissionError(
            "Serato DJ is running. It overwrites library files when it quits, so changes made now would be lost or "
            "could corrupt the library. Quit Serato first (the quit_serato tool can do it), or pass force: true if "
            "you accept the risk."
        )


def backup_base() -> str:
    return os.environ.get("SERATO_MCP_BACKUP_DIR") or str(Path.home() / ".serato-mcp" / "backups")


def _read(file: str) -> bytes:
    with open(file, "rb") as fh:
        return fh.read()


class WriteTxn:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.stamp = re.sub(r"[:.]", "-", now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z")
        self.backup_dir = os.path.join(backup_base(), self.stamp)
        self.changed: list[str] = []
        self._backed_up: set[str] = set()

    def backup(self, file: str) -> None:
        if file in self._backed_up or not os.path.exists(file):
            return
        # Mirror the file's absolute path inside the backup folder (with the drive letter as a folder on Windows).
        absolute = Path(os.path.abspath(file))
        dest = os.path.join(self.backup_dir, absolute.drive.rstrip(":"), *absolute.parts[1:])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(file, dest)
        self._backed_up.add(file)

    def write(self, file: str, data: bytes) -> None:
        self.backup(file)
        os.makedirs(os.path.dirname(file), exist_ok=True)
        tmp = f"{file}.serato-mcp-{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, file)
        self.changed.append(file)

    def remove(self, file: str) -> None:
        """Delete a file; its backup copy stays in the backup folder."""
        self.backup(file)
        os.unlink(file)
        self.changed.append(file)

    def summary(self) -> dict:
        return {"changedFiles": self.changed, "backupDir": self.backup_dir if self._backed_up else None}


# ---- crates ------------------------------------------------------------------------

DEFAULT_COLUMNS = [
    ("song", "250"), ("artist", "250"), ("bpm", "30"), ("key", "30"), ("album", "250"), ("length", "250"),
    ("comment", "250"),
]  # fmt: skip


def _new_crate_fields() -> list[Field]:
    fields = [make_field("vrsn", "1.0/Serato ScratchLive Crate")]
    for name, width in DEFAULT_COLUMNS:
        fields.append(Field("ovct", b"", [make_field("tvcn", name), make_field("tvcw", width)]))
    return fields


def _track_field(stored: str) -> Field:
    return Field("otrk", b"", [make_field("ptrk", stored)])


def _crate_stored_paths(fields: list[Field]) -> list[str]:
    out = []
    for f in fields:
        p = get_child(f, "ptrk") if f.tag == "otrk" else None
        if p:
            out.append(decode_utf16be(p.raw))
    return out


def _group_by_root(lib: Library, paths: list[str]) -> list[tuple[SeratoRoot, list[str]]]:
    """Group absolute track paths by the Serato root (drive) they belong to, in first-seen order."""
    out: list[tuple[SeratoRoot, list[str]]] = []
    for p in paths:
        r = root_for_path(lib.roots, p)
        group = next((g for g in out if g[0] is r), None)
        if group:
            group[1].append(p)
        else:
            out.append((r, [p]))
    return out


def _crate_line(name: str) -> str:
    return "[crate]" + "%%".join(s.strip() for s in name.split("/"))


def _update_crate_order(
    txn: WriteTxn, root: SeratoRoot, display_name: str, op: str, rename_to: str | None = None
) -> None:
    file = os.path.join(root.dir, "neworder.pref")
    if not os.path.exists(file):
        return
    lines = decode_utf16be(_read(file)).split("\n")
    line = _crate_line(display_name)
    if op == "add":
        if line in lines:
            return
        end = next((i for i, x in enumerate(lines) if x.startswith("[end record]")), -1)
        lines = list(lines)
        lines.insert(end if end >= 0 else len(lines), line)
    elif rename_to:
        to = _crate_line(rename_to)
        lines = [to + x[len(line) :] if x == line or x.startswith(line + "%%") else x for x in lines]
    else:
        lines = [x for x in lines if not (x == line or x.startswith(line + "%%"))]
    txn.write(file, encode_utf16be("\n".join(lines)))


def _find_crate(lib: Library, name: str) -> Crate | None:
    try:
        return lib.crate(name)
    except ValueError:
        return None


def _legacy_crate_files(lib: Library, file_name: str) -> list[tuple[SeratoRoot, str]]:
    """Legacy crate files (one per drive) for a crate file name."""
    files = [(root, os.path.join(root.dir, "Subcrates", file_name)) for root in lib.roots]
    return [(r, f) for r, f in files if os.path.exists(f)]


def _not_serato(crate: Crate) -> ValueError:
    return ValueError(f'"{crate.name}" is a {crate.space} {crate.kind}; only Serato crates can be edited')


def _editable_crate(lib: Library, name: str) -> tuple[Crate, str, list[tuple[SeratoRoot, str]]]:
    crate = lib.crate(name)
    if crate.space and crate.space != "Serato Library":
        raise _not_serato(crate)
    file_name = crate.fileName or crate_file_name(crate.name)
    return crate, file_name, _legacy_crate_files(lib, file_name)


def _import_note(lib: Library) -> dict:
    if lib.backend != "sqlite":
        return {}
    return {
        "note": "Written to Serato's library files. Serato DJ 4 imports the change the next time it starts; until "
        "then the crate shows as pendingImport."
    }


def write_crate_tracks(lib: Library, name: str, track_paths: list[str], mode: str, force: bool | None = None) -> dict:
    """mode: create | replace | append."""
    assert_writable(force)
    file_name = crate_file_name(name)
    txn = WriteTxn()
    streaming = [p for p in track_paths if is_streaming(p)]
    track_paths = [p for p in track_paths if not is_streaming(p)]
    existing = _find_crate(lib, name)
    if existing and existing.space and existing.space != "Serato Library":
        raise _not_serato(existing)
    if mode == "create" and existing:
        raise ValueError(f'Crate "{existing.name}" already exists. Use mode "append" or "replace".')
    # Serato 4 crate never exported to a legacy file: start from its current tracks so the import doesn't drop them.
    if mode == "append" and existing and not _legacy_crate_files(lib, file_name):
        track_paths = [p for p in existing.trackPaths if not is_streaming(p)] + track_paths
    groups = _group_by_root(lib, track_paths)
    if not groups:  # a crate with no tracks still needs a file on the home library
        groups = [(lib.roots[0], [])]
    added = skipped = total = 0
    for root, paths in groups:
        file = os.path.join(root.dir, "Subcrates", file_name)
        exists = os.path.exists(file)
        if mode == "create" and exists:
            raise ValueError(f'Crate "{name}" already exists. Use mode "append" or "replace".')
        fields = decode(_read(file)) if exists and mode == "append" else _new_crate_fields()
        if exists and mode == "replace":
            fields = [f for f in decode(_read(file)) if f.tag != "otrk"]  # keep the user's column layout
        present = set(_crate_stored_paths(fields))
        for p in paths:
            stored = to_stored(root, p)
            if stored in present:
                skipped += 1
                continue
            present.add(stored)
            fields.append(_track_field(stored))
            added += 1
        total += len(present)
        txn.write(file, encode(fields))
        if not exists:
            # Make sure parent crates exist so Serato shows the nesting.
            parts = file_name.removesuffix(".crate").split("%%")
            for i in range(1, len(parts)):
                parent = os.path.join(root.dir, "Subcrates", "%%".join(parts[:i]) + ".crate")
                if not os.path.exists(parent):
                    txn.write(parent, encode(_new_crate_fields()))
            _update_crate_order(txn, root, name, "add")
    lib.refresh(True)
    out = {
        "crate": name,
        "files": txn.changed,
        "added": added,
        "skippedDuplicates": skipped,
        "totalTracks": total,
        "backupDir": txn.summary()["backupDir"],
    }
    if streaming:
        out["skippedStreamingTracks"] = len(streaming)
    return {**out, **_import_note(lib)}


def remove_from_crate(lib: Library, name: str, track_paths: list[str], force: bool | None = None) -> dict:
    assert_writable(force)
    crate, _, files = _editable_crate(lib, name)
    if not files:
        # Serato 4 crate without a legacy file: write one with the remaining tracks.
        drop = set(track_paths)
        result = write_crate_tracks(lib, crate.name, [p for p in crate.trackPaths if p not in drop], "replace", force)
        return {**result, "removed": sum(1 for p in crate.trackPaths if p in drop)}
    txn = WriteTxn()
    removed = total = 0
    for root, file in files:
        drop = {to_stored(root, p) for p in track_paths if not is_streaming(p) and root_for_path(lib.roots, p) is root}
        fields = decode(_read(file))
        kept = []
        for f in fields:
            p = get_child(f, "ptrk") if f.tag == "otrk" else None
            if p and decode_utf16be(p.raw) in drop:
                removed += 1
            else:
                kept.append(f)
        total += sum(1 for f in kept if f.tag == "otrk")
        if len(kept) != len(fields):
            txn.write(file, encode(kept))
    lib.refresh(True)
    return {
        "crate": crate.name,
        "files": txn.changed,
        "removed": removed,
        "totalTracks": total,
        "backupDir": txn.summary()["backupDir"],
        **_import_note(lib),
    }


def delete_crate(lib: Library, name: str, force: bool | None = None) -> dict:
    assert_writable(force)
    crate, file_name, files = _editable_crate(lib, name)
    if not files:
        raise ValueError(
            f'"{crate.name}" has no crate file in _Serato_/Subcrates to delete. Delete it inside Serato DJ instead.'
        )
    prefix = file_name.removesuffix(".crate")
    txn = WriteTxn()
    for root in lib.roots:
        d = os.path.join(root.dir, "Subcrates")
        if not os.path.exists(d):
            continue
        for f in sorted(os.listdir(d)):
            if f == file_name or f.startswith(prefix + "%%"):
                txn.remove(os.path.join(d, f))
        _update_crate_order(txn, root, "/".join(crate.name.split(" / ")), "remove")
    lib.refresh(True)
    return {"crate": crate.name, "files": txn.changed, "backupDir": txn.summary()["backupDir"], **_import_note(lib)}


def rename_crate(lib: Library, name: str, new_name: str, force: bool | None = None) -> dict:
    assert_writable(force)
    crate, file_name, files = _editable_crate(lib, name)
    if not files:
        raise ValueError(
            f'"{crate.name}" has no crate file in _Serato_/Subcrates to rename. Rename it inside Serato DJ instead.'
        )
    old_prefix = file_name.removesuffix(".crate")
    new_prefix = crate_file_name(new_name).removesuffix(".crate")
    txn = WriteTxn()
    for root in lib.roots:
        d = os.path.join(root.dir, "Subcrates")
        if not os.path.exists(d):
            continue
        for f in sorted(os.listdir(d)):
            if f == file_name or f.startswith(old_prefix + "%%"):
                target = os.path.join(d, new_prefix + f[len(old_prefix) :])
                if os.path.exists(target):
                    raise ValueError(f"A crate already exists at {os.path.basename(target)}")
                txn.backup(os.path.join(d, f))
                os.rename(os.path.join(d, f), target)
                txn.changed.append(target)
        _update_crate_order(txn, root, "/".join(crate.name.split(" / ")), "remove", new_name)
    lib.refresh(True)
    return {"crate": new_name, "files": txn.changed, "backupDir": txn.summary()["backupDir"], **_import_note(lib)}


# ---- database V2 -----------------------------------------------------------------------

EDITABLE_FIELDS = {
    "title": "tsng", "artist": "tart", "album": "talb", "genre": "tgen", "comment": "tcom", "grouping": "tgrp",
    "remixer": "trmx", "composer": "tcmp", "label": "tlbl", "year": "ttyr", "key": "tkey", "bpm": "tbpm",
}  # fmt: skip


def _db_track(fields: list[Field], stored: str) -> Field | None:
    for f in fields:
        p = get_child(f, "pfil") if f.tag == "otrk" else None
        if p and decode_utf16be(p.raw) == stored:
            return f
    return None


def _is_number(v: str) -> bool:
    try:
        n = float(v)
    except ValueError:
        return False
    return n == n and abs(n) != float("inf")


def edit_tracks(lib: Library, edits: list[tuple[str, dict[str, str]]], force: bool | None = None) -> dict:
    """edits: (absolute path, {field: value}); an empty value clears the field."""
    assert_writable(force)
    txn = WriteTxn()
    results = []
    by_root: list[tuple[SeratoRoot, list[tuple[str, dict[str, str]]]]] = []
    for path, changes in edits:
        r = root_for_path(lib.roots, path)
        group = next((g for g in by_root if g[0] is r), None)
        if group:
            group[1].append((path, changes))
        else:
            by_root.append((r, [(path, changes)]))
    for root, items in by_root:
        file = os.path.join(root.dir, "database V2")
        fields = decode(_read(file))
        for path, changes in items:
            t = _db_track(fields, to_stored(root, path))
            if not t:
                raise ValueError(
                    f"{path} is only in Serato DJ 4's library, not in the legacy database V2 that serato-mcp can edit. "
                    "Edit it inside Serato (or its file tags) instead."
                    if lib.backend == "sqlite"
                    else f"Track not in Serato database: {path}"
                )
            updated = []
            for k, v in changes.items():
                if v is None:
                    continue
                if k == "color":
                    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", v)
                    if not m:
                        raise ValueError(f"color must be #rrggbb, got {v}")
                    set_child(t, "ulbl", int(m.group(1), 16))
                else:
                    tag = EDITABLE_FIELDS.get(k)
                    if not tag:
                        raise ValueError(f"Field {k} is not editable")
                    if k == "bpm" and v != "" and not _is_number(v):
                        raise ValueError(f"bpm must be a number, got {v}")
                    if v == "":
                        remove_child(t, tag)
                    else:
                        set_child(t, tag, to_fixed(float(v), 2) if k == "bpm" else v)
                updated.append(k)
            results.append({"path": path, "updated": updated})
        txn.write(file, encode(fields))
    lib.refresh(True)
    return {"results": results, **txn.summary(), **_import_note(lib)}


# ---- relocating moved files ----------------------------------------------------------------

AUDIO = re.compile(r"\.(mp3|m4a|mp4|aac|alac|flac|wav|wave|aif|aiff|ogg)$", re.I)


def _match_key(name: str) -> str:
    return re.sub(r"[\s_]+", " ", unicodedata.normalize("NFC", name).lower())


def _loose_key(name: str) -> str:
    s = re.sub(r"\.[a-z0-9]+$", "", _match_key(name))
    s = re.sub(r"\((original|extended|clean extended|radio|dirty extended)( mix| edit)?\)", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def relocate_missing(lib: Library, search_dirs: list[str], dry_run: bool, force: bool | None = None) -> dict:
    """Find moved/renamed files for tracks Serato reports as missing and repoint the database and every crate at
    the new location. Matches by file name, using the stored file size to break ties."""
    missing = [t for t in lib.tracks if t.missing]
    index: dict[str, list[str]] = {}

    def walk(d: str, depth: int) -> None:
        if depth > 8:
            return
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for e in entries:
            if e.startswith(".") or e in ("_Serato_", "_Serato_Backup"):
                continue
            full = os.path.join(d, e)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                continue
            if is_dir:
                walk(full, depth + 1)
            elif AUDIO.search(e):
                index.setdefault(_match_key(e), []).append(full)

    for d in search_dirs:
        walk(d, 0)

    matches: list[dict] = []
    unresolved: list[str] = []
    for t in missing:
        cands = index.get(_match_key(os.path.basename(t.path)), [])
        confidence = "exact-name"
        if not cands:
            # Same name without the "(Extended Mix)"-style suffix, e.g. "Eynka - Symphony.mp3"
            loose = _loose_key(os.path.basename(t.path))
            cands = [p for k, v in index.items() if _loose_key(k) == loose for p in v]
            confidence = "fuzzy-name"
        if len(cands) > 1 and t.sizeBytes:
            sized = [c for c in cands if os.path.getsize(c) == t.sizeBytes]
            if sized:
                cands = sized
                if confidence == "exact-name":
                    confidence = "name+size"
        if len(cands) == 1 and not any(m["to"] == cands[0] for m in matches):
            track = f"{t.artist or ''} - {t.title or os.path.basename(t.path)}"
            matches.append({"track": track, "from": t.path, "to": cands[0], "confidence": confidence})
        else:
            unresolved.append(t.path)
    if dry_run or not matches:
        return {"dryRun": dry_run, "missing": len(missing), "matched": matches, "unresolved": unresolved}

    assert_writable(force)
    txn = WriteTxn()
    for root in lib.roots:
        mapping: dict[str, str] = {}
        for m in matches:
            if root_for_path(lib.roots, m["from"]) is not root:
                continue
            try:
                mapping[to_stored(root, m["from"])] = to_stored(root, m["to"])
            except ValueError:
                unresolved.append(m["from"])  # moved to another drive: Serato can't follow that
        if not mapping:
            continue
        db = os.path.join(root.dir, "database V2")
        if os.path.exists(db):
            fields = decode(_read(db))
            existing = {
                decode_utf16be(get_child(f, "pfil").raw) for f in fields if f.tag == "otrk" and get_child(f, "pfil")
            }
            for f in fields:
                p = get_child(f, "pfil") if f.tag == "otrk" else None
                to = mapping.get(decode_utf16be(p.raw)) if p else None
                if to and to not in existing:
                    set_child(f, "pfil", to)
            # If the new location is already its own entry, drop the stale duplicate.
            cleaned = []
            for f in fields:
                p = get_child(f, "pfil") if f.tag == "otrk" else None
                stored = decode_utf16be(p.raw) if p else None
                if stored is not None and stored in mapping and mapping[stored] in existing:
                    continue
                cleaned.append(f)
            txn.write(db, encode(cleaned))
        d = os.path.join(root.dir, "Subcrates")
        if os.path.exists(d):
            for cf in sorted(x for x in os.listdir(d) if x.endswith(".crate")):
                file = os.path.join(d, cf)
                fields = decode(_read(file))
                touched = False
                for f in fields:
                    p = get_child(f, "ptrk") if f.tag == "otrk" else None
                    to = mapping.get(decode_utf16be(p.raw)) if p else None
                    if to:
                        set_child(f, "ptrk", to)
                        touched = True
                if touched:
                    txn.write(file, encode(fields))
    lib.refresh(True)
    return {"dryRun": False, "missing": len(missing), "relocated": matches, "unresolved": unresolved, **txn.summary()}

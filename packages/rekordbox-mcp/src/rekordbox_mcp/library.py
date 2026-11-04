"""Thin, MCP-agnostic wrapper around pyrekordbox's MasterDatabase (rekordbox 6/7)."""

from __future__ import annotations

import functools
import os
import shutil
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .camelot import bpm_compatible, compatible_camelot, to_camelot

HOT_CUE_LETTERS = "ABCDEFGH"


class WriteNotAllowed(RuntimeError):
    pass


def _s(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    return str(v) if not isinstance(v, (int, float, bool, str)) else v


def track_summary(c: Any) -> dict[str, Any]:
    key = c.KeyName
    return {
        "id": str(c.ID),
        "title": c.Title,
        "artist": c.ArtistName,
        "bpm": round(c.BPM / 100, 2) if c.BPM else None,
        "key": key,
        "camelot": to_camelot(key),
        "genre": c.GenreName,
        "duration_sec": c.Length,
        "rating": c.Rating,
    }


def track_detail(c: Any) -> dict[str, Any]:
    d = track_summary(c)
    d.update(
        album=c.AlbumName,
        album_artist=c.AlbumArtistName,
        remixer=c.RemixerName,
        composer=c.ComposerName,
        label=c.LabelName,
        year=c.ReleaseYear,
        comment=c.Commnt,
        color=c.ColorName,
        play_count=c.DJPlayCount,
        date_added=_s(c.DateCreated),
        path=c.FolderPath,
        file_type=c.FileType,
        bitrate=c.BitRate,
        sample_rate=c.SampleRate,
        my_tags=list(c.MyTagNames or []),
        cues=[cue_dict(q) for q in sorted(c.Cues, key=lambda q: (q.Kind or 0, q.InMsec or 0))],
    )
    return d


def cue_dict(q: Any) -> dict[str, Any]:
    kind = q.Kind or 0
    out_ms = q.OutMsec if q.OutMsec is not None and q.OutMsec >= 0 else None
    return {
        "type": "memory" if kind == 0 else "hot",
        "slot": HOT_CUE_LETTERS[kind - 1] if 0 < kind <= len(HOT_CUE_LETTERS) else None,
        "position_sec": round((q.InMsec or 0) / 1000, 3),
        "loop_end_sec": round(out_ms / 1000, 3) if out_ms is not None else None,
        "comment": q.Comment or None,
    }


def _operation(fn):
    """Start each public operation on a fresh transaction so reads see whatever rekordbox wrote since."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self.db.session.rollback()
        return fn(self, *args, **kwargs)

    return wrapper


class RekordboxLibrary:
    """Lazily opens the encrypted master.db and exposes high-level operations."""

    def __init__(self, db_dir: str | None = None, allow_write: bool | None = None):
        self._db_dir = db_dir or os.environ.get("REKORDBOX_DB_DIR") or ""
        if allow_write is None:
            allow_write = os.environ.get("REKORDBOX_MCP_ALLOW_WRITE", "").lower() in ("1", "true", "yes")
        self.allow_write = allow_write
        self._db = None
        self._backed_up = False

    # ---------------------------------------------------------------- plumbing
    @property
    def db(self):
        if self._db is None:
            from pyrekordbox.masterdb import MasterDatabase

            if self._db_dir:
                # NB: pyrekordbox's `db_dir` only relocates side files; `path` picks the database itself.
                path = Path(self._db_dir).expanduser() / "master.db"
                db = MasterDatabase(path=path, db_dir=path.parent)
                if Path(db.engine.url.database).resolve() != path.resolve():
                    db.close()
                    raise RuntimeError(f"Opened {db.engine.url.database}, expected {path}")
                self._db = db
            else:
                self._db = MasterDatabase()
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @staticmethod
    def rekordbox_running() -> bool:
        from pyrekordbox.utils import get_rekordbox_pid

        return bool(get_rekordbox_pid())

    def _content(self, track_id: str | int):
        c = self.db.get_content(ID=str(track_id))
        if c is None:
            raise ValueError(f"No track with id {track_id}")
        return c

    def _playlist(self, playlist_id: str | int):
        p = self.db.get_playlist(ID=str(playlist_id))
        if p is None:
            raise ValueError(f"No playlist with id {playlist_id}")
        return p

    # ------------------------------------------------------------------- reads
    @_operation
    def stats(self) -> dict[str, Any]:
        tracks = self.db.get_content().all()
        bpms = [t.BPM / 100 for t in tracks if t.BPM]
        return {
            "db_directory": str(self.db.db_directory),
            "rekordbox_running": self.rekordbox_running(),
            "writes_enabled": self.allow_write,
            "track_count": len(tracks),
            "playlist_count": sum(1 for p in self.db.get_playlist() if not p.is_folder),
            "bpm_range": [round(min(bpms), 2), round(max(bpms), 2)] if bpms else None,
            "top_genres": Counter(t.GenreName for t in tracks if t.GenreName).most_common(10),
            "top_keys": Counter(to_camelot(t.KeyName) for t in tracks if t.KeyName).most_common(12),
            "top_artists": Counter(t.ArtistName for t in tracks if t.ArtistName).most_common(10),
        }

    @_operation
    def search_tracks(
        self,
        query: str | None = None,
        artist: str | None = None,
        genre: str | None = None,
        key: str | None = None,
        bpm_min: float | None = None,
        bpm_max: float | None = None,
        min_rating: int | None = None,
        my_tag: str | None = None,
        sort_by: str = "title",
        limit: int = 50,
    ) -> dict[str, Any]:
        q = (query or "").lower()
        camelot = to_camelot(key) if key else None
        if key and camelot is None:
            raise ValueError(f"Unrecognised key {key!r}; use e.g. 'Am', 'F#', or '8A'")

        def match(c) -> bool:
            if q:
                hay = " ".join(
                    str(x or "") for x in (c.Title, c.ArtistName, c.AlbumName, c.RemixerName, c.LabelName, c.Commnt)
                ).lower()
                if q not in hay:
                    return False
            if artist and artist.lower() not in (c.ArtistName or "").lower():
                return False
            if genre and genre.lower() not in (c.GenreName or "").lower():
                return False
            if camelot and to_camelot(c.KeyName) != camelot:
                return False
            bpm = (c.BPM or 0) / 100
            if bpm_min is not None and bpm < bpm_min:
                return False
            if bpm_max is not None and bpm > bpm_max:
                return False
            if min_rating is not None and (c.Rating or 0) < min_rating:
                return False
            return not my_tag or my_tag.lower() in [t.lower() for t in (c.MyTagNames or [])]

        hits = [c for c in self.db.get_content() if match(c)]
        hits = _sort(hits, sort_by)
        return {
            "total": len(hits),
            "returned": min(len(hits), limit),
            "tracks": [track_summary(c) for c in hits[:limit]],
        }

    @_operation
    def get_track(self, track_id: str) -> dict[str, Any]:
        return track_detail(self._content(track_id))

    @_operation
    def list_playlists(self) -> list[dict[str, Any]]:
        pls = self.db.get_playlist().all()
        by_parent: dict[str, list] = {}
        for p in pls:
            by_parent.setdefault(str(p.ParentID), []).append(p)

        def node(p) -> dict[str, Any]:
            kind = "folder" if p.is_folder else "smart_playlist" if p.is_smart_playlist else "playlist"
            d: dict[str, Any] = {"id": str(p.ID), "name": p.Name, "type": kind}
            if p.is_folder:
                d["children"] = [node(ch) for ch in sorted(by_parent.get(str(p.ID), []), key=lambda x: x.Seq or 0)]
            elif not p.is_smart_playlist:
                d["track_count"] = len(p.Songs)
            return d

        return [node(p) for p in sorted(by_parent.get("root", []), key=lambda x: x.Seq or 0)]

    @_operation
    def get_playlist_tracks(self, playlist_id: str, limit: int = 500) -> dict[str, Any]:
        p = self._playlist(playlist_id)
        if p.is_folder:
            raise ValueError(f"{p.Name!r} is a folder, not a playlist")
        if p.is_smart_playlist:
            contents = self.db.get_playlist_contents(p).all()
            tracks = [track_summary(c) for c in contents]
        else:
            songs = sorted(p.Songs, key=lambda s: s.TrackNo or 0)
            tracks = [dict(track_summary(s.Content), position=s.TrackNo) for s in songs if s.Content]
        return {"id": str(p.ID), "name": p.Name, "total": len(tracks), "tracks": tracks[:limit]}

    @_operation
    def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        hs = [h for h in self.db.get_history() if not h.Attribute]
        hs.sort(key=lambda h: h.DateCreated or "", reverse=True)
        return [
            {
                "id": str(h.ID),
                "name": h.Name,
                "date": _s(h.DateCreated),
                "track_count": self.db.get_history_songs(HistoryID=h.ID).count(),
            }
            for h in hs[:limit]
        ]

    @_operation
    def get_history_tracks(self, history_id: str) -> dict[str, Any]:
        h = self.db.get_history(ID=str(history_id))
        if h is None:
            raise ValueError(f"No history session with id {history_id}")
        songs = sorted(self.db.get_history_songs(HistoryID=h.ID).all(), key=lambda s: s.TrackNo or 0)
        return {
            "id": str(h.ID),
            "name": h.Name,
            "tracks": [dict(track_summary(s.Content), position=s.TrackNo) for s in songs if s.Content],
        }

    @_operation
    def list_my_tags(self) -> list[dict[str, Any]]:
        tags = self.db.get_my_tag().all()
        cats = [t for t in tags if t.Attribute == 1]
        return [
            {
                "category": c.Name,
                "tags": [t.Name for t in sorted(tags, key=lambda x: x.Seq or 0) if str(t.ParentID) == str(c.ID)],
            }
            for c in sorted(cats, key=lambda x: x.Seq or 0)
        ]

    @_operation
    def find_compatible_tracks(
        self,
        track_id: str,
        bpm_tolerance_pct: float = 6.0,
        energy_boost: bool = False,
        same_genre: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        seed = self._content(track_id)
        seed_cam = to_camelot(seed.KeyName)
        seed_bpm = (seed.BPM or 0) / 100
        keys = set(compatible_camelot(seed_cam, energy_boost)) if seed_cam else None
        out = []
        for c in self.db.get_content():
            if c.ID == seed.ID:
                continue
            cam = to_camelot(c.KeyName)
            if keys is not None and cam not in keys:
                continue
            bpm = (c.BPM or 0) / 100
            if seed_bpm and not bpm_compatible(seed_bpm, bpm, bpm_tolerance_pct):
                continue
            if same_genre and (c.GenreName or "").lower() != (seed.GenreName or "").lower():
                continue
            d = track_summary(c)
            d["bpm_diff_pct"] = round((bpm - seed_bpm) / seed_bpm * 100, 1) if seed_bpm and bpm else None
            d["same_key"] = cam == seed_cam
            out.append(d)
        out.sort(key=lambda d: (not d["same_key"], abs(d["bpm_diff_pct"] or 0)))
        return {
            "seed": track_summary(seed),
            "compatible_keys": sorted(keys) if keys else None,
            "total": len(out),
            "tracks": out[:limit],
        }

    @_operation
    def find_duplicates(self) -> list[dict[str, Any]]:
        groups: dict[tuple, list] = {}
        for c in self.db.get_content():
            k = ((c.Title or "").strip().lower(), (c.ArtistName or "").strip().lower())
            if k[0]:
                groups.setdefault(k, []).append(c)
        return [
            {
                "title": g[0].Title,
                "artist": g[0].ArtistName,
                "copies": [dict(track_summary(c), path=c.FolderPath) for c in g],
            }
            for g in groups.values()
            if len(g) > 1
        ]

    @_operation
    def build_set(
        self,
        track_ids: list[str] | None = None,
        playlist_id: str | None = None,
        curve: str = "build",
        max_tracks: int | None = None,
    ) -> dict[str, Any]:
        from .sequencing import SetTrack, build_set_list

        if bool(track_ids) == bool(playlist_id):
            raise ValueError("Give either track_ids or playlist_id")
        if playlist_id:
            p = self._playlist(playlist_id)
            if p.is_folder:
                raise ValueError(f"{p.Name!r} is a folder, not a playlist")
            contents = (
                self.db.get_playlist_contents(p).all()
                if p.is_smart_playlist
                else [s.Content for s in sorted(p.Songs, key=lambda s: s.TrackNo or 0) if s.Content]
            )
        else:
            contents = [self._content(t) for t in dict.fromkeys(track_ids)]
        if max_tracks:
            contents = contents[:max_tracks]
        tracks = [
            SetTrack(
                str(c.ID),
                c.Title or "",
                c.ArtistName or "",
                round(c.BPM / 100, 2) if c.BPM else None,
                to_camelot(c.KeyName),
            )
            for c in contents
        ]
        return build_set_list(tracks, curve)

    # ------------------------------------------------------------------ writes
    def _check_write(self) -> None:
        if not self.allow_write:
            raise WriteNotAllowed(
                "Writes are disabled. Set REKORDBOX_MCP_ALLOW_WRITE=1 in the MCP server's environment to enable them."
            )
        if self.rekordbox_running():
            raise WriteNotAllowed("rekordbox is running. Quit rekordbox before modifying the library.")
        self._backup()

    def _backup(self) -> None:
        if self._backed_up:
            return
        src_dir = Path(self.db.db_directory)
        dest = src_dir / "rekordbox-mcp-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
            if (src_dir / name).exists():
                shutil.copy2(src_dir / name, dest / name)
        self._backed_up = True

    def _commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @_operation
    def create_playlist(self, name: str, parent_id: str | None = None, track_ids: Iterable[str] = ()) -> dict[str, Any]:
        self._check_write()
        track_ids = list(track_ids)
        try:
            parent = self._playlist(parent_id) if parent_id else None
            pl = self.db.create_playlist(name, parent=parent)
            for tid in track_ids:
                self.db.add_to_playlist(pl, self._content(tid))
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"id": str(pl.ID), "name": pl.Name, "track_count": len(track_ids)}

    @_operation
    def create_playlist_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        self._check_write()
        try:
            parent = self._playlist(parent_id) if parent_id else None
            f = self.db.create_playlist_folder(name, parent=parent)
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"id": str(f.ID), "name": f.Name}

    @_operation
    def add_tracks_to_playlist(
        self, playlist_id: str, track_ids: list[str], skip_existing: bool = True
    ) -> dict[str, Any]:
        self._check_write()
        added, skipped = [], []
        try:
            pl = self._playlist(playlist_id)
            if pl.is_folder or pl.is_smart_playlist:
                raise ValueError(f"{pl.Name!r} is not a regular playlist")
            existing = {str(s.ContentID) for s in pl.Songs}
            for tid in track_ids:
                c = self._content(tid)
                if skip_existing and str(c.ID) in existing:
                    skipped.append(str(c.ID))
                    continue
                self.db.add_to_playlist(pl, c)
                existing.add(str(c.ID))
                added.append(str(c.ID))
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"playlist_id": str(pl.ID), "added": added, "skipped_already_present": skipped}

    @_operation
    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
        self._check_write()
        wanted = {str(t) for t in track_ids}
        removed = []
        try:
            pl = self._playlist(playlist_id)
            for s in list(pl.Songs):
                if str(s.ContentID) in wanted:
                    self.db.remove_from_playlist(pl, s)
                    removed.append(str(s.ContentID))
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"playlist_id": str(pl.ID), "removed": removed, "not_found": sorted(wanted - set(removed))}

    @_operation
    def rename_playlist(self, playlist_id: str, name: str) -> dict[str, Any]:
        self._check_write()
        try:
            pl = self._playlist(playlist_id)
            self.db.rename_playlist(pl, name)
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"id": str(pl.ID), "name": name}

    @_operation
    def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        self._check_write()
        try:
            pl = self._playlist(playlist_id)
            name = pl.Name
            self.db.delete_playlist(pl)
        except Exception:
            self.db.rollback()
            raise
        self._commit()
        return {"deleted_id": str(playlist_id), "name": name}


_SORTS = {
    "title": lambda c: (c.Title or "").lower(),
    "artist": lambda c: ((c.ArtistName or "").lower(), (c.Title or "").lower()),
    "bpm": lambda c: c.BPM or 0,
    "key": lambda c: (int((to_camelot(c.KeyName) or "99Z")[:-1]), (to_camelot(c.KeyName) or "Z")[-1]),
    "date_added": lambda c: str(c.DateCreated or ""),
    "rating": lambda c: -(c.Rating or 0),
    "play_count": lambda c: -(c.DJPlayCount or 0),
}


def _sort(items: list, sort_by: str) -> list:
    if sort_by not in _SORTS:
        raise ValueError(f"sort_by must be one of {sorted(_SORTS)}")
    rev = sort_by == "date_added"
    return sorted(items, key=_SORTS[sort_by], reverse=rev)

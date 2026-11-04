"""MCP server exposing a local rekordbox 6/7 library."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .library import RekordboxLibrary, WriteNotAllowed

# pyrekordbox logs a warning to stderr on every open while rekordbox is running; that is expected.
logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

mcp = MCPServer(
    "rekordbox",
    instructions=(
        "Access the user's local Pioneer rekordbox library (tracks, playlists, cues, history, My Tags). "
        "Track and playlist IDs are strings. BPM is in beats per minute; keys are given in both classical "
        "and Camelot notation. Write tools only work when REKORDBOX_MCP_ALLOW_WRITE=1 and rekordbox is closed; "
        "a backup of master.db is taken before the first write of each session."
    ),
)

lib = RekordboxLibrary()

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def tool(annotations: ToolAnnotations) -> Callable[[Callable], Callable]:
    """Register a tool, surfacing expected failures (bad IDs, writes disabled, rekordbox open) to the model."""

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, WriteNotAllowed) as e:
                raise ToolError(str(e)) from e
            except RuntimeError as e:
                if "rekordbox" in str(e).lower():
                    raise ToolError(str(e)) from e
                raise

        return mcp.tool(annotations=annotations)(wrapper)

    return decorate


# ------------------------------------------------------------------- read tools
@tool(READ)
def library_stats() -> dict[str, Any]:
    """Library summary: track/playlist counts, BPM range, top genres/keys/artists, and whether writes are possible."""
    return lib.stats()


@tool(READ)
def search_tracks(
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
    """Search the track collection. All filters are optional and combined with AND.

    query: case-insensitive substring over title, artist, album, remixer, label and comment.
    artist / genre: case-insensitive substring match.
    key: exact key in classical ("Am", "F#") or Camelot ("8A") notation.
    bpm_min / bpm_max: inclusive BPM range.
    min_rating: 0-5 stars.
    my_tag: exact My Tag name (e.g. "Vocal").
    sort_by: title | artist | bpm | key | date_added | rating | play_count.
    """
    return lib.search_tracks(query, artist, genre, key, bpm_min, bpm_max, min_rating, my_tag, sort_by, limit)


@tool(READ)
def get_track(track_id: str) -> dict[str, Any]:
    """Full metadata for one track, including file path, My Tags, memory cues and hot cues."""
    return lib.get_track(track_id)


@tool(READ)
def list_playlists() -> list[dict[str, Any]]:
    """The playlist tree (folders, playlists, smart playlists) with IDs and track counts."""
    return lib.list_playlists()


@tool(READ)
def get_playlist_tracks(playlist_id: str, limit: int = 500) -> dict[str, Any]:
    """Tracks in a playlist or smart playlist, in playlist order."""
    return lib.get_playlist_tracks(playlist_id, limit)


@tool(READ)
def list_history(limit: int = 50) -> list[dict[str, Any]]:
    """Recent play-history sessions (newest first)."""
    return lib.list_history(limit)


@tool(READ)
def get_history_tracks(history_id: str) -> dict[str, Any]:
    """Tracks played in a history session, in play order."""
    return lib.get_history_tracks(history_id)


@tool(READ)
def list_my_tags() -> list[dict[str, Any]]:
    """All My Tag categories and their tags."""
    return lib.list_my_tags()


@tool(READ)
def find_compatible_tracks(
    track_id: str,
    bpm_tolerance_pct: float = 6.0,
    energy_boost: bool = False,
    same_genre: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    """Suggest tracks that mix well after `track_id`: harmonically compatible on the Camelot wheel
    (same key, +/-1, relative major/minor; energy_boost adds +2 and +7) and within the BPM tolerance
    (half/double time counts). Same-key and closest-BPM matches come first."""
    return lib.find_compatible_tracks(track_id, bpm_tolerance_pct, energy_boost, same_genre, limit)


@tool(READ)
def build_set(
    track_ids: list[str] | None = None,
    playlist_id: str | None = None,
    curve: str = "build",
    max_tracks: int | None = None,
) -> dict[str, Any]:
    """Order tracks into a mixable set: harmonic key moves, small tempo changes, and an energy curve.

    Give either track_ids or a playlist_id. curve: build (rising BPM), peak (up then down),
    cooldown (falling BPM) or smooth (easiest transitions). Each transition is graded smooth/workable/hard.
    Read-only: to save the result, pass the ordered ids to create_playlist.
    """
    return lib.build_set(track_ids, playlist_id, curve, max_tracks)


@tool(READ)
def find_duplicate_tracks() -> list[dict[str, Any]]:
    """Groups of tracks that share the same title and artist (likely duplicate imports)."""
    return lib.find_duplicates()


# ------------------------------------------------------------------ write tools
@tool(WRITE)
def create_playlist(
    name: str, parent_folder_id: str | None = None, track_ids: list[str] | None = None
) -> dict[str, Any]:
    """Create a playlist (optionally inside a folder) and fill it with tracks.

    Requires writes enabled and rekordbox closed."""
    return lib.create_playlist(name, parent_folder_id, track_ids or [])


@tool(WRITE)
def create_playlist_folder(name: str, parent_folder_id: str | None = None) -> dict[str, Any]:
    """Create a playlist folder. Requires writes enabled and rekordbox closed."""
    return lib.create_playlist_folder(name, parent_folder_id)


@tool(WRITE)
def add_tracks_to_playlist(playlist_id: str, track_ids: list[str], skip_existing: bool = True) -> dict[str, Any]:
    """Append tracks to the end of a playlist. Requires writes enabled and rekordbox closed."""
    return lib.add_tracks_to_playlist(playlist_id, track_ids, skip_existing)


@tool(DESTRUCTIVE)
def remove_tracks_from_playlist(playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
    """Remove tracks from a playlist (they stay in the collection). Requires writes enabled and rekordbox closed."""
    return lib.remove_tracks_from_playlist(playlist_id, track_ids)


@tool(WRITE)
def rename_playlist(playlist_id: str, name: str) -> dict[str, Any]:
    """Rename a playlist or folder. Requires writes enabled and rekordbox closed."""
    return lib.rename_playlist(playlist_id, name)


@tool(DESTRUCTIVE)
def delete_playlist(playlist_id: str) -> dict[str, Any]:
    """Delete a playlist or folder and its children. Tracks stay in the collection.

    Requires writes enabled and rekordbox closed."""
    return lib.delete_playlist(playlist_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

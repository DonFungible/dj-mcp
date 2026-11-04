"""MCP server for Spotify playlists / Liked Songs, plus building DJ sets from them via the rekordbox library."""

from __future__ import annotations

import argparse
import functools
import logging
import statistics
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .auth import AuthError, TokenStore
from .client import SpotifyClient, SpotifyError

logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

mcp = MCPServer(
    "spotify",
    instructions=(
        "Read the user's Spotify playlists and Liked Songs, create Spotify playlists, and build DJ sets. "
        "build_mix matches Spotify songs to the user's local rekordbox library (which has BPM and key; Spotify no "
        "longer provides them) and orders the matches into a mixable set. Spotify tracks can't be played in DJ "
        "software, so a set is only DJ-able from the matched local files. To save a set to rekordbox, pass its "
        "rekordbox_track_ids to the rekordbox server's create_playlist tool; to save it to Spotify, pass "
        "spotify_uris to create_playlist here."
    ),
)
_client: SpotifyClient | None = None


def client() -> SpotifyClient:
    global _client
    if _client is None:
        _client = SpotifyClient()
    return _client


READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def tool(annotations: ToolAnnotations) -> Callable[[Callable], Callable]:
    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (SpotifyError, AuthError, ValueError) as e:
                raise ToolError(str(e)) from e

        return mcp.tool(annotations=annotations)(wrapper)

    return decorate


@tool(READ)
def spotify_status() -> dict[str, Any]:
    """Whether the server is logged in to Spotify, and as whom."""
    token = client().store.load()
    if not token:
        return {
            "logged_in": False,
            "help": "Run `uvx spotify-dj-mcp login` in a terminal (with SPOTIFY_CLIENT_ID set).",
        }
    return {"logged_in": True, "user": client().me(), "scopes": token.get("scope", "").split()}


@tool(READ)
def list_playlists(limit: int = 100) -> list[dict[str, Any]]:
    """The user's playlists (own and followed) with IDs and track counts."""
    return client().playlists(limit)


@tool(READ)
def get_playlist_tracks(playlist: str, limit: int = 500) -> dict[str, Any]:
    """Tracks in a playlist. `playlist` can be an ID, a spotify:playlist: URI, or an open.spotify.com link."""
    tracks = client().playlist_tracks(playlist, limit)
    return {"playlist": client().playlist(playlist), "returned": len(tracks), "tracks": tracks}


@tool(READ)
def get_liked_songs(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """The user's Liked Songs, most recently liked first."""
    tracks = client().liked_songs(limit, offset)
    return {"offset": offset, "returned": len(tracks), "tracks": tracks}


@tool(READ)
def search_spotify(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search Spotify for tracks (max 10 results; Spotify caps this for personal apps)."""
    return client().search_tracks(query, limit)


@tool(WRITE)
def create_playlist(name: str, track_uris: list[str], description: str = "", public: bool = False) -> dict[str, Any]:
    """Create a new Spotify playlist (private by default) containing the given spotify:track: URIs, in order."""
    return client().create_playlist(name, track_uris, description, public)


# ------------------------------------------------------------------ DJ-set building
def _collect(sources: list[str], liked_limit: int, playlist_limit: int) -> tuple[list[dict], list[str]]:
    seen, out, labels = set(), [], []
    for src in sources:
        if src.strip().lower() in ("liked", "liked songs", "saved"):
            tracks, label = client().liked_songs(liked_limit), "Liked Songs"
        else:
            info = client().playlist(src)
            tracks, label = client().playlist_tracks(src, playlist_limit), info["name"]
        labels.append(f"{label} ({len(tracks)})")
        for t in tracks:
            key = t["id"] or (t["name"].lower(), tuple(a.lower() for a in t["artists"]))
            if key not in seen:
                seen.add(key)
                out.append(t)
    return out, labels


def _rekordbox_tracks() -> list[dict]:
    from rekordbox_mcp.library import RekordboxLibrary

    lib = RekordboxLibrary(allow_write=False)
    try:
        return lib.search_tracks(limit=10**9)["tracks"]
    except Exception as e:  # noqa: BLE001 - surface any library problem as a readable tool error
        raise SpotifyError(f"Couldn't read the rekordbox library: {e}") from e
    finally:
        lib.close()


def match_tracks(spotify_tracks: list[dict], library: list[dict]) -> tuple[list[tuple], list[dict], list[dict]]:
    from .matching import Matcher

    by_id = {str(t["id"]): t for t in library}
    matcher = Matcher(library)
    matched, uncertain, missing, used = [], [], [], set()
    for s in spotify_tracks:
        m = matcher.match(s["name"], s["artists"])
        ref = {"spotify_name": s["name"], "spotify_artists": ", ".join(s["artists"]), "spotify_uri": s["uri"]}
        if m is None:
            missing.append(ref)
        elif m.confidence == "different version":
            uncertain.append(
                dict(ref, rekordbox_id=m.track.id, rekordbox_title=m.track.title, rekordbox_artist=m.track.artist)
            )
        elif m.track.id not in used:
            used.add(m.track.id)
            matched.append((s, by_id[m.track.id], m.confidence))
    return matched, uncertain, missing


def _pick_tempo_band(items: list[tuple], max_tracks: int) -> list[tuple]:
    """Keep the max_tracks tracks closest to the median BPM, so the set stays in one tempo range."""
    with_bpm = [x for x in items if x[1].get("bpm")]
    if len(items) <= max_tracks or not with_bpm:
        return items[:max_tracks]
    med = statistics.median(x[1]["bpm"] for x in with_bpm)

    def dist(x):
        bpm = x[1].get("bpm")
        return min(abs(c - med) for c in (bpm, bpm * 2, bpm / 2)) if bpm else 1e9

    return sorted(items, key=dist)[:max_tracks]


@tool(READ)
def build_mix(
    sources: list[str] | None = None,
    curve: str = "build",
    max_tracks: int = 30,
    include_different_versions: bool = False,
    liked_limit: int = 500,
    playlist_limit: int = 1000,
) -> dict[str, Any]:
    """Build a DJ set from Spotify songs the user also owns in rekordbox.

    sources: "liked" and/or playlist IDs/links (default ["liked"]).
    curve: build (rising BPM), peak (up then down), cooldown (falling), smooth (easiest transitions).
    max_tracks: set length; if more songs match, the ones nearest the median tempo are used.
    include_different_versions: also use library tracks that look like a different version (e.g. original
    vs a remix) of the Spotify song.

    Returns the ordered set (with graded transitions), the rekordbox ids and Spotify URIs in set order, songs
    that only matched a different version, and songs not found in the library (a shopping list).
    Read-only: save with rekordbox create_playlist(rekordbox_track_ids) and/or create_playlist(spotify_uris).
    """
    from rekordbox_mcp.camelot import to_camelot
    from rekordbox_mcp.sequencing import SetTrack, build_set_list

    sources = sources or ["liked"]
    spotify_tracks, labels = _collect(sources, liked_limit, playlist_limit)
    library = _rekordbox_tracks()
    matched, uncertain, missing = match_tracks(spotify_tracks, library)
    if include_different_versions:
        by_id = {str(t["id"]): t for t in library}
        used = {r[1]["id"] for r in matched}
        for u in uncertain:
            if u["rekordbox_id"] not in used:
                used.add(u["rekordbox_id"])
                spotify = {"uri": u["spotify_uri"], "name": u["spotify_name"]}
                matched.append((spotify, by_id[u["rekordbox_id"]], "different version"))
        uncertain = []
    chosen = _pick_tempo_band(matched, max_tracks)
    set_tracks = [
        SetTrack(
            id=rb["id"],
            title=rb["title"] or "",
            artist=rb["artist"] or "",
            bpm=rb.get("bpm"),
            camelot=rb.get("camelot") or to_camelot(rb.get("key")),
            extra=(("spotify_uri", sp.get("uri")), ("match", conf)),
        )
        for sp, rb, conf in chosen
    ]
    result = build_set_list(set_tracks, curve) if set_tracks else {"curve": curve, "track_count": 0, "tracks": []}
    result.update(
        sources=labels,
        summary={
            "spotify_songs": len(spotify_tracks),
            "matched_in_rekordbox": len(matched),
            "used_in_set": len(set_tracks),
            "different_version_only": len(uncertain),
            "not_in_library": len(missing),
        },
        rekordbox_track_ids=[t["id"] for t in result["tracks"]],
        spotify_uris=[
            t["spotify_uri"] for t in result["tracks"] if str(t.get("spotify_uri", "")).startswith("spotify:track:")
        ],
        different_version_only=uncertain,
        not_in_library=missing,
    )
    return result


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> None:
    from . import auth

    ap = argparse.ArgumentParser(prog="spotify-dj-mcp", description="Spotify MCP server")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    p = sub.add_parser("login", help="log in to Spotify in your browser")
    p.add_argument("--no-browser", action="store_true", help="print the login link instead of opening it")
    sub.add_parser("status", help="show login status")
    sub.add_parser("logout", help="delete the saved Spotify token")
    args = ap.parse_args(argv)

    try:
        if args.command == "login":
            auth.login(open_browser=not args.no_browser)
            print(f"Logged in as {SpotifyClient().me()['display_name']}. Token saved to {auth.TOKEN_PATH}")
        elif args.command == "status":
            print(spotify_status())
        elif args.command == "logout":
            print("Logged out." if TokenStore().clear() else "Wasn't logged in.")
        else:
            mcp.run()
    except (AuthError, SpotifyError) as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()

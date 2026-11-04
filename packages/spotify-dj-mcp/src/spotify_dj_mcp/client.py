"""Minimal Spotify Web API client for the endpoints this server needs.

Targets the February 2026 API: playlist contents at /playlists/{id}/items with each entry's track under
"item" (formerly "track"), playlist creation at POST /me/playlists, and playlist objects whose track
count lives under "items" (formerly "tracks"). Old field names are still accepted.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator

import httpx

from .auth import AuthError, TokenStore, client_id, refresh

API = "https://api.spotify.com/v1"
MAX_RETRIES = 3


class SpotifyError(RuntimeError):
    pass


class NotLoggedIn(SpotifyError):
    pass


def playlist_id_from(ref: str) -> str:
    """Accept a playlist ID, spotify:playlist:ID URI, or open.spotify.com URL."""
    ref = ref.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{10,})", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", ref):
        return ref
    raise SpotifyError(f"Not a Spotify playlist ID, URI or link: {ref!r}")


def track_summary(t: dict) -> dict | None:
    if not t or t.get("type") not in (None, "track"):
        return None  # podcast episodes etc.
    return {
        "id": t.get("id"),
        "uri": t.get("uri"),
        "name": t.get("name") or "",
        "artists": [a.get("name", "") for a in t.get("artists") or []],
        "album": (t.get("album") or {}).get("name"),
        "duration_sec": round((t.get("duration_ms") or 0) / 1000),
        "explicit": t.get("explicit"),
        "is_local": bool(t.get("is_local")),
    }


def entry_track(entry: dict) -> dict | None:
    """A playlist/library entry's track: 'item' in the 2026 API, 'track' before."""
    return entry.get("item") or entry.get("track")


def playlist_summary(p: dict) -> dict:
    count = (p.get("items") or p.get("tracks") or {}).get("total")
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "owner": (p.get("owner") or {}).get("display_name") or (p.get("owner") or {}).get("id"),
        "track_count": count,
        "public": p.get("public"),
        "url": (p.get("external_urls") or {}).get("spotify"),
    }


class SpotifyClient:
    def __init__(self, store: TokenStore | None = None, http: httpx.Client | None = None, cid: str | None = None):
        self.store = store or TokenStore()
        self.http = http or httpx.Client(timeout=20)
        self._cid = cid

    @property
    def cid(self) -> str:
        if self._cid is None:
            self._cid = client_id()
        return self._cid

    # ---------------------------------------------------------------- auth
    def _token(self, force_refresh: bool = False) -> str:
        token = self.store.load()
        if not token:
            raise NotLoggedIn("Not logged in to Spotify. Run `uvx spotify-dj-mcp login` in a terminal first.")
        if force_refresh or token.get("expires_at", 0) < time.time() + 60:
            try:
                token = refresh(self.cid, token, self.http)
            except AuthError as e:
                raise NotLoggedIn(str(e)) from e
            self.store.save(token)
        return token["access_token"]

    # ------------------------------------------------------------- requests
    def request(self, method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else API + path
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token(force_refresh=refreshed)}"}
            resp = self.http.request(method, url, headers=headers, **kw)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                time.sleep(min(float(resp.headers.get("Retry-After", "1")), 10))
                continue
            if resp.status_code >= 400:
                raise SpotifyError(self._error_message(resp))
            return resp.json() if resp.content else {}
        raise SpotifyError("Spotify kept rate-limiting the request; try again in a minute.")

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            msg = resp.json().get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except ValueError:
            msg = resp.text[:200]
        hint = ""
        if resp.status_code == 403:
            hint = (
                " (Development Mode apps need the owner on Spotify Premium, and the user added under"
                " User Management in the app dashboard; some endpoints are closed to new apps.)"
            )
        return f"Spotify API {resp.status_code}: {msg or resp.reason_phrase}{hint}"

    def paginate(self, path: str, params: dict | None = None, max_items: int | None = None) -> Iterator[dict]:
        url, count = path, 0
        params = dict(params or {})
        while url:
            page = self.request("GET", url, params=params)
            params = None  # the "next" URL already carries the query
            for item in page.get("items") or []:
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            url = page.get("next")

    # ------------------------------------------------------------ endpoints
    def me(self) -> dict:
        u = self.request("GET", "/me")
        return {"id": u.get("id"), "display_name": u.get("display_name")}

    def playlists(self, limit: int = 50) -> list[dict]:
        return [playlist_summary(p) for p in self.paginate("/me/playlists", {"limit": 50}, limit) if p]

    def playlist(self, ref: str) -> dict:
        return playlist_summary(self.request("GET", f"/playlists/{playlist_id_from(ref)}"))

    def playlist_tracks(self, ref: str, limit: int = 1000) -> list[dict]:
        pid = playlist_id_from(ref)
        out = []
        for entry in self.paginate(f"/playlists/{pid}/items", {"limit": 100}, limit):
            t = track_summary(entry_track(entry))
            if t:
                out.append(t)
        return out

    def liked_songs(self, limit: int = 500, offset: int = 0) -> list[dict]:
        out = []
        for entry in self.paginate("/me/tracks", {"limit": 50, "offset": offset}, limit):
            t = track_summary(entry_track(entry))
            if t:
                t["added_at"] = entry.get("added_at")
                out.append(t)
        return out

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        data = self.request("GET", "/search", params={"q": query, "type": "track", "limit": max(1, min(limit, 10))})
        return [t for t in (track_summary(x) for x in (data.get("tracks") or {}).get("items") or []) if t]

    def create_playlist(self, name: str, uris: list[str], description: str = "", public: bool = False) -> dict:
        bad = [u for u in uris if not str(u).startswith("spotify:track:")]
        if bad:
            raise SpotifyError(f"Only spotify:track: URIs can be added; got {bad[:3]}")
        p = self.request("POST", "/me/playlists", json={"name": name, "description": description, "public": public})
        for i in range(0, len(uris), 100):
            self.request("POST", f"/playlists/{p['id']}/items", json={"uris": uris[i : i + 100]})
        out = playlist_summary(p)
        out["track_count"] = len(uris)
        return out

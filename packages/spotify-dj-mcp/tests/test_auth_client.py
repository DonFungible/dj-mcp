import base64
import hashlib
import json
import os
import stat
import sys
import time

import httpx
import pytest

from spotify_dj_mcp import auth
from spotify_dj_mcp import client as client_mod
from spotify_dj_mcp.auth import TokenStore, authorize_url, pkce_pair, refresh
from spotify_dj_mcp.client import NotLoggedIn, SpotifyClient, SpotifyError, playlist_id_from


def test_pkce_pair():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url():
    url = authorize_url("cid", "http://127.0.0.1:8888/callback", "chal", "st")
    assert "code_challenge_method=S256" in url and "client_id=cid" in url and "user-library-read" in url


def test_token_store_is_private(tmp_path):
    store = TokenStore(tmp_path / "t" / "token.json")
    store.save({"access_token": "a"})
    if sys.platform != "win32":  # Windows has no Unix permission bits
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load() == {"access_token": "a"}
    assert store.clear() and store.load() is None


def test_refresh_keeps_old_refresh_token():
    def handler(req):
        assert b"grant_type=refresh_token" in req.content
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    tok = refresh("cid", {"refresh_token": "r1"}, httpx.Client(transport=httpx.MockTransport(handler)))
    assert tok["access_token"] == "new" and tok["refresh_token"] == "r1"


def test_load_dotenv_only_spotify_keys(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SPOTIFY_CLIENT_ID=abc123\nOTHER_SECRET=nope\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("OTHER_SECRET", raising=False)
    auth.load_dotenv()
    assert os.environ["SPOTIFY_CLIENT_ID"] == "abc123"
    assert "OTHER_SECRET" not in os.environ


def test_playlist_id_from():
    assert playlist_id_from("37i9dQZF1DXcBWIGoYBM5M") == "37i9dQZF1DXcBWIGoYBM5M"
    assert playlist_id_from("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == "37i9dQZF1DXcBWIGoYBM5M"
    assert playlist_id_from("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x") == "37i9dQZF1DXcBWIGoYBM5M"
    with pytest.raises(SpotifyError):
        playlist_id_from("not a playlist!")


# ---------------------------------------------------------------- fake Spotify API
def trk(i, name, artist="DJ", type_="track"):
    return {
        "id": f"t{i}",
        "uri": f"spotify:track:t{i}",
        "name": name,
        "type": type_,
        "artists": [{"name": artist}],
        "album": {"name": "A"},
        "duration_ms": 200000,
    }


class FakeSpotify:
    def __init__(self, old_shape=False):
        self.calls = []
        self.old = old_shape
        self.fail_401_once = False
        self.rate_limit_once = False

    def __call__(self, req: httpx.Request):
        path = req.url.path.removeprefix("/v1")
        self.calls.append((req.method, path))
        if self.fail_401_once:
            self.fail_401_once = False
            return httpx.Response(401, json={"error": {"message": "expired"}})
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, headers={"Retry-After": "0"})
        key = "track" if self.old else "item"
        if req.method == "GET" and path == "/me/playlists":
            count_key = "tracks" if self.old else "items"
            if "offset=50" in str(req.url):
                return httpx.Response(
                    200, json={"items": [{"id": "p51", "name": "Last", count_key: {"total": 1}}], "next": None}
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": f"p{i}", "name": f"P{i}", count_key: {"total": i}, "owner": {"display_name": "me"}}
                        for i in range(50)
                    ],
                    "next": "https://api.spotify.com/v1/me/playlists?offset=50&limit=50",
                },
            )
        if req.method == "GET" and path.startswith("/playlists/") and path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {key: trk(1, "One")},
                        {key: trk(2, "Pod", type_="episode")},
                        {key: None},
                        {key: trk(3, "Three")},
                    ],
                    "next": None,
                },
            )
        if req.method == "GET" and path == "/me/tracks":
            return httpx.Response(
                200, json={"items": [{"added_at": "2026-01-01", "track": trk(9, "Liked")}], "next": None}
            )
        if req.method == "POST" and path == "/me/playlists":
            return httpx.Response(201, json={"id": "newp", "name": json.loads(req.content)["name"]})
        if req.method == "POST" and path == "/playlists/newp/items":
            return httpx.Response(201, json={"snapshot_id": "s"})
        if path == "/forbidden":
            return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})


def make_client(tmp_path, fake, token=None):
    store = TokenStore(tmp_path / "token.json")
    store.save(token or {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600})
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return SpotifyClient(store=store, http=http, cid="cid")


@pytest.mark.parametrize("old", [False, True])
def test_reads_new_and_old_shapes(tmp_path, old):
    c = make_client(tmp_path, FakeSpotify(old_shape=old))
    pls = c.playlists(limit=100)
    assert len(pls) == 51 and pls[3]["track_count"] == 3 and pls[-1]["name"] == "Last"
    tracks = c.playlist_tracks("spotify:playlist:abcdefghij")
    assert [t["name"] for t in tracks] == ["One", "Three"]  # episode and null entries skipped
    liked = c.liked_songs()
    assert liked[0]["name"] == "Liked" and liked[0]["added_at"] == "2026-01-01"


def test_create_playlist_uses_2026_endpoints_and_chunks(tmp_path):
    fake = FakeSpotify()
    c = make_client(tmp_path, fake)
    uris = [f"spotify:track:t{i}" for i in range(230)]
    res = c.create_playlist("Mix", uris)
    assert res["id"] == "newp" and res["track_count"] == 230
    assert ("POST", "/me/playlists") in fake.calls
    assert fake.calls.count(("POST", "/playlists/newp/items")) == 3
    with pytest.raises(SpotifyError, match="spotify:track"):
        c.create_playlist("Bad", ["spotify:episode:x"])


def test_401_refreshes_and_retries(tmp_path, monkeypatch):
    fake = FakeSpotify()
    fake.fail_401_once = True
    monkeypatch.setattr(
        client_mod, "refresh", lambda cid, tok, http: dict(tok, access_token="b", expires_at=time.time() + 3600)
    )
    c = make_client(tmp_path, fake)
    assert c.liked_songs()[0]["name"] == "Liked"
    assert c.store.load()["access_token"] == "b"


def test_429_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    fake = FakeSpotify()
    fake.rate_limit_once = True
    assert make_client(tmp_path, fake).liked_songs()[0]["name"] == "Liked"


def test_expired_token_refreshes_before_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        client_mod, "refresh", lambda cid, tok, http: dict(tok, access_token="fresh", expires_at=time.time() + 3600)
    )
    c = make_client(tmp_path, FakeSpotify(), token={"access_token": "old", "refresh_token": "r", "expires_at": 0})
    c.liked_songs()
    assert c.store.load()["access_token"] == "fresh"


def test_errors(tmp_path):
    c = make_client(tmp_path, FakeSpotify())
    with pytest.raises(SpotifyError, match="Premium"):
        c.request("GET", "/forbidden")
    c.store.clear()
    with pytest.raises(NotLoggedIn, match="spotify-dj-mcp login"):
        c.liked_songs()

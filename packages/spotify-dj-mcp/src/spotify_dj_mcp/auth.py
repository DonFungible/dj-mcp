"""Spotify OAuth (Authorization Code + PKCE: needs a client ID, no secret) and token storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = [
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]
TOKEN_PATH = Path.home() / ".config" / "spotify-dj-mcp" / "token.json"


class AuthError(RuntimeError):
    pass


def load_dotenv() -> None:
    """Fill SPOTIFY_* variables from the nearest .env (cwd upwards, then the repo this package lives in)."""
    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve()):
        for d in [start, *start.parents]:
            env = d / ".env"
            if env in seen or not env.is_file():
                continue
            seen.add(env)
            for line in env.read_text().splitlines():
                k, sep, v = line.strip().partition("=")
                if sep and k.startswith("SPOTIFY_") and k not in os.environ:
                    os.environ[k] = v.strip().strip("\"'")
            break


def client_id() -> str:
    load_dotenv()
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not cid:
        raise AuthError(
            "SPOTIFY_CLIENT_ID is not set. Create an app at https://developer.spotify.com/dashboard, then set "
            "SPOTIFY_CLIENT_ID in the environment (e.g. the MCP server's env config) or in a .env file."
        )
    return cid


def redirect_uri() -> str:
    load_dotenv()
    return os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cid: str, redirect: str, challenge: str, state: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": cid,
                "response_type": "code",
                "redirect_uri": redirect,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state,
                "scope": " ".join(SCOPES),
            }
        )
    )


class TokenStore:
    def __init__(self, path: Path = TOKEN_PATH):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token, f)
        os.chmod(self.path, 0o600)

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


def _token_from_response(resp: httpx.Response, previous: dict | None = None) -> dict:
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            detail = resp.text[:200]
        raise AuthError(f"Spotify token request failed ({resp.status_code}): {detail}")
    data = resp.json()
    token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
        "scope": data.get("scope", ""),
    }
    return token


def exchange_code(cid: str, code: str, verifier: str, redirect: str, http: httpx.Client) -> dict:
    resp = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    return _token_from_response(resp)


def refresh(cid: str, token: dict, http: httpx.Client) -> dict:
    if not token.get("refresh_token"):
        raise AuthError("No refresh token saved. Run `uvx spotify-dj-mcp login` again.")
    resp = http.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": cid},
    )
    try:
        return _token_from_response(resp, previous=token)
    except AuthError as e:
        raise AuthError(f"{e}. Run `uvx spotify-dj-mcp login` again.") from e


def login(store: TokenStore | None = None, open_browser: bool = True, timeout: float = 300) -> dict:
    """Interactive browser login. Starts a one-shot callback server on the redirect URI."""
    store = store or TokenStore()
    cid, redirect = client_id(), redirect_uri()
    target = urlparse(redirect)
    if target.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise AuthError(f"Redirect URI must be a loopback address, got {redirect}")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != target.path:
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            ok = q.get("state") == state and "code" in q
            result.update(q, ok=ok)
            body = (
                "<h2>Logged in to Spotify.</h2><p>You can close this tab.</p>"
                if ok
                else f"<h2>Spotify login failed</h2><p>{q.get('error', 'state mismatch')}</p>"
            )
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    server = HTTPServer((target.hostname, target.port or 80), Callback)
    server.timeout = 1
    url = authorize_url(cid, redirect, challenge, state)
    print(f"Opening Spotify login. If the browser doesn't open, visit:\n{url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + timeout
    try:
        while "ok" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "ok" not in result:
        raise AuthError("Timed out waiting for the Spotify login.")
    if not result["ok"]:
        raise AuthError(f"Spotify login failed: {result.get('error', 'state mismatch')}")
    with httpx.Client(timeout=20) as http:
        token = exchange_code(cid, result["code"], verifier, redirect, http)
    store.save(token)
    return token

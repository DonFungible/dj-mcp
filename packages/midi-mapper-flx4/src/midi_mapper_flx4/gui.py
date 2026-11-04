"""Local web GUI for midi-mapper-flx4.

Security: the GUI can create shell-command mappings, so it binds to 127.0.0.1 only, requires a random
per-session token on every API call, and rejects foreign Host/Origin headers (blocks CSRF and DNS rebinding).
"""

from __future__ import annotations

import json
import queue
import secrets
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .config import ConfigError, control_from_json, mapping_from_json
from .runtime import Runtime

ACCESSIBILITY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, runtime: Runtime, port: int = 0):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(24)
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/#token={self.token}"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


class Handler(BaseHTTPRequestHandler):
    server: GuiServer

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    # --------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        if self.headers.get("Host") not in self.server.allowed_hosts():
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.removeprefix("http://") in self.server.allowed_hosts()

    def _authorised(self, query: dict) -> bool:
        token = self.headers.get("X-Token") or (query.get("token") or [""])[0]
        return self._host_ok() and secrets.compare_digest(token, self.server.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            raise ConfigError("request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            raise ConfigError("invalid JSON") from e
        if not isinstance(data, dict):
            raise ConfigError("expected a JSON object")
        return data

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "forbidden")
            html = resources.files("midi_mapper_flx4").joinpath("web/index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._authorised(query):
            return self._error(HTTPStatus.FORBIDDEN, "forbidden")
        if url.path == "/api/state":
            return self._json(self.server.runtime.state())
        if url.path == "/api/events":
            return self._events()
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._authorised({}):  # POSTs must use the header, never the query string
            return self._error(HTTPStatus.FORBIDDEN, "forbidden")
        rt = self.server.runtime
        try:
            body = self._body()
            if url.path == "/api/mappings":
                replace = control_from_json(body["replace"]) if body.get("replace") else None
                rt.save_mapping(mapping_from_json(body), replace=replace)
            elif url.path == "/api/mappings/delete":
                rt.delete_mapping(control_from_json(body.get("control") or {}))
            elif url.path == "/api/enabled":
                rt.set_enabled(bool(body.get("enabled")))
            elif url.path == "/api/learn":
                rt.set_learning(bool(body.get("active")))
            elif url.path == "/api/open-accessibility":
                subprocess.Popen(["open", ACCESSIBILITY_SETTINGS])
            else:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
        except ConfigError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        self._json(rt.state())

    def _events(self) -> None:
        rt = self.server.runtime
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = rt.subscribe()
        try:
            self.wfile.write(f"data: {json.dumps(rt.state())}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            rt.unsubscribe(q)


def serve(runtime: Runtime, port: int = 0, open_browser: bool = True) -> None:
    import webbrowser

    from .engine import log

    server = GuiServer(runtime, port)
    runtime.start()
    log(f"GUI at {server.url}")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.stop()

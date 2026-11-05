"""Long-running mapper shared by `run` and `gui`: device (re)connection, config reloads, live events."""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from pathlib import Path

from . import service
from .config import Config, Control, Mapping, load_config, parse_config, render_config
from .engine import Executor, Mapper, PauseWatcher, input_names, log
from .flx4 import control_id, control_name

POLL_SECONDS = 2.0
# While the controller is idle, reopen its connection this often. A quick replug or DJ software resetting the
# FLX4 can leave a connection with the same name that silently delivers nothing; reopening costs milliseconds.
REFRESH_IDLE = 20.0
LEARN_TIMEOUT = 60.0  # learning auto-expires so a closed browser tab can't leave mappings suppressed
EVENT_MIN_INTERVAL = 0.05  # per-control throttle for jog/fader streams sent to the GUI


class Runtime:
    def __init__(self, path: Path, dry_run: bool = False, verbose: bool = True):
        from . import midiwatch

        midiwatch.start()  # see midiwatch: without it, replugs and DJ-software resets go unnoticed
        self.path = Path(path)
        self.config: Config = load_config(self.path)
        self.mapper = Mapper(self.config, Executor(dry_run=dry_run))
        self.dry_run = dry_run
        self.verbose = verbose
        self.enabled = True
        self.port_name: str | None = None
        self._port = None
        self._learn_until = 0.0
        self._mtime = self.path.stat().st_mtime
        self._lock = threading.RLock()
        self._listeners: list[queue.Queue] = []
        self._last_emit: dict[Control, float] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.devices: list[str] = []
        self._reconnect = False
        self._quiet_refresh = False
        self._last_activity = time.monotonic()
        self.watcher = PauseWatcher(self.config.pause_when_running, on_change=self._on_pause_change)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        from . import permissions

        service.claim(self.path)
        self._can_type = permissions.can_send_keys()
        if self._can_type is False and any(service.needs_keystrokes(m) for m in self.config.mappings.values()):
            permissions.request_send_keys()  # macOS shows its prompt for the app that started the mapper
            log("asked macOS for Accessibility permission (needed for keystroke and scroll mappings)")
        threading.Thread(target=self._device_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        service.release(self.path)
        if self._port is not None:
            self._port.close()

    def _device_loop(self) -> None:
        import mido

        from . import permissions

        while not self._stop.is_set():
            try:
                can_type = permissions.can_send_keys()
                if can_type != getattr(self, "_can_type", can_type):
                    self._can_type = can_type
                    service.write_status(self.path, can_type)
                    log("keystrokes allowed" if can_type else "keystrokes blocked by macOS")
                self._maybe_reload()
                names = input_names()
                if names != self.devices:
                    self.devices = names
                    self.emit_state()
                wanted = next((n for n in names if self.config.device.lower() in n.lower()), None)
                idle = time.monotonic() - self._last_activity > REFRESH_IDLE
                if self._port is not None and wanted == self.port_name and idle:
                    self._reconnect = True  # quiet refresh; see REFRESH_IDLE
                if self._port is not None and (wanted != self.port_name or self._reconnect):
                    self._port.close()
                    self._port, self.port_name = None, None
                    if not self._reconnect:
                        log("controller disconnected")
                        self.emit_state()
                self._quiet_refresh = self._reconnect and idle
                self._reconnect = False
                if self._port is None and wanted:
                    self._port = mido.open_input(wanted, callback=self.on_message)
                    self.port_name = wanted
                    self._last_activity = time.monotonic()
                    if not self._quiet_refresh:
                        log(f"listening on {wanted} with {len(self.config.mappings)} mapping(s)")
                        self.emit_state()
            except Exception as e:  # noqa: BLE001 - keep retrying
                log("device check failed:", e)
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _on_pause_change(self) -> None:
        # DJ software resets the controller, and macOS then hands out a fresh MIDI connection under the same name,
        # leaving ours silently dead. Reopen it whenever the DJ app closes.
        watcher = getattr(self, "watcher", None)  # unset when the DJ app is already open at startup
        if watcher is not None and not watcher.paused and self._port is not None:
            self._reconnect = True
            self._wake.set()
            log("reconnecting to the controller after the DJ app closed")
        self.emit_state()

    # ----------------------------------------------------------------- state
    @property
    def learning(self) -> bool:
        return time.monotonic() < self._learn_until or service.learning_active(self.path)

    def set_learning(self, on: bool) -> None:
        self._learn_until = time.monotonic() + LEARN_TIMEOUT if on else 0.0
        self.emit_state()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        log("enabled" if on else "disabled")
        self.emit_state()

    def state(self) -> dict:
        from .config import mapping_to_json
        from .engine import accessibility_trusted

        return {
            "type": "state",
            "device": self.config.device,
            "port": self.port_name,
            "devices": self.devices,
            "connected": self.port_name is not None,
            "paused_by": self.watcher.running_app,
            "enabled": self.enabled,
            "learning": self.learning,
            "dry_run": self.dry_run,
            "accessibility": accessibility_trusted(),
            "config_path": str(self.path),
            "mappings": [
                {**mapping_to_json(m), "id": control_id(m.control), "control_name": control_name(m.control)}
                for m in self.config.mappings.values()
            ],
        }

    # ------------------------------------------------------------ config edits
    def _apply(self, cfg: Config) -> None:
        text = render_config(cfg)
        cfg = parse_config(text)  # round-trip validation before touching the file
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(text)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime
        self.config = cfg
        self.mapper.config = cfg
        self.emit_state()

    def save_mapping(self, m: Mapping, replace: Control | None = None) -> None:
        with self._lock:
            mappings = dict(self.config.mappings)
            if replace is not None:
                mappings.pop(replace, None)
            mappings[m.control] = m
            self._apply(Config(self.config.device, self.config.pause_when_running, mappings))

    def delete_mapping(self, ctrl: Control) -> None:
        with self._lock:
            mappings = {k: v for k, v in self.config.mappings.items() if k != ctrl}
            self._apply(Config(self.config.device, self.config.pause_when_running, mappings))

    def _maybe_reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        with self._lock:
            self._mtime = mtime
            try:
                cfg = load_config(self.path)
            except Exception as e:  # noqa: BLE001 - keep the previous working config
                log(f"config not reloaded, keeping previous one: {e}")
                return
            self.config = cfg
            self.mapper.config = cfg
            log(f"reloaded config: {len(cfg.mappings)} mapping(s)")
        self.emit_state()

    # ---------------------------------------------------------------- events
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    def emit(self, event: dict) -> None:
        for q in list(self._listeners):
            with contextlib.suppress(queue.Full):
                q.put_nowait(event)

    def emit_state(self) -> None:
        if self._listeners:
            self.emit(self.state())

    def on_message(self, msg) -> None:
        self._last_activity = time.monotonic()
        self._maybe_reload()
        ctrl = Control.from_message(msg)
        if ctrl is None:
            return
        mapping = self.config.mappings.get(ctrl)
        fired = False
        if self.learning:
            pass  # pressing a control to learn it must not trigger its current action
        elif self.enabled and not self.watcher.paused:
            try:
                fired = self.mapper.handle(msg) is not None
            except Exception as e:  # noqa: BLE001 - one bad action shouldn't stop the mapper
                log(f"error in {mapping.label if mapping else ctrl}: {e}")
                self.emit({"type": "error", "message": f"{mapping.label if mapping else ctrl}: {e}"})
            if fired and self.verbose and mapping.scroll is None and mapping.keys_cw is None:
                log(f"→ {mapping.label}")

        now = time.monotonic()
        if ctrl.kind == "cc" and now - self._last_emit.get(ctrl, 0) < EVENT_MIN_INTERVAL and not self.learning:
            return
        self._last_emit[ctrl] = now
        if self._listeners:
            self.emit(
                {
                    "type": "learn" if self.learning else "midi",
                    "control": {"kind": ctrl.kind, "channel": ctrl.channel, "number": ctrl.number},
                    "control_name": control_name(ctrl),
                    "value": msg.value if ctrl.kind == "cc" else msg.velocity,
                    "mapped": mapping.label if mapping else None,
                    "fired": fired,
                }
            )

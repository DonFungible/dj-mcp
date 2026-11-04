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
LEARN_TIMEOUT = 60.0  # learning auto-expires so a closed browser tab can't leave mappings suppressed
EVENT_MIN_INTERVAL = 0.05  # per-control throttle for jog/fader streams sent to the GUI


class Runtime:
    def __init__(self, path: Path, dry_run: bool = False, verbose: bool = True):
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
        self.watcher = PauseWatcher(self.config.pause_when_running, on_change=self.emit_state)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        service.claim(self.path)
        threading.Thread(target=self._device_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        service.release(self.path)
        if self._port is not None:
            self._port.close()

    def _device_loop(self) -> None:
        import mido

        while not self._stop.is_set():
            try:
                self._maybe_reload()
                names = input_names()
                if names != self.devices:
                    self.devices = names
                    self.emit_state()
                wanted = next((n for n in names if self.config.device.lower() in n.lower()), None)
                if self._port is not None and wanted != self.port_name:
                    self._port.close()
                    self._port, self.port_name = None, None
                    log("controller disconnected")
                    self.emit_state()
                if self._port is None and wanted:
                    self._port = mido.open_input(wanted, callback=self.on_message)
                    self.port_name = wanted
                    log(f"listening on {wanted} with {len(self.config.mappings)} mapping(s)")
                    self.emit_state()
            except Exception as e:  # noqa: BLE001 - keep retrying
                log("device check failed:", e)
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

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

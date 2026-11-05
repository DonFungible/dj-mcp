"""Turning MIDI messages into actions."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config, Control, Mapping, parse_combo
from .flx4 import find_mapping

SHELL_LOG = Path.home() / ".config" / "midi-mapper-flx4" / "shell.log"


def app_command(name: str) -> list[str]:
    """Command that opens an application, or brings it to the front if it's already open."""
    if sys.platform == "darwin":
        return ["open", "-a", name]
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "", name]
    return [name.lower()]


MAX_STEPS_PER_MESSAGE = 10  # cap repeats from one fast jog flick


def log(*parts: object) -> None:
    print(f"{datetime.now():%H:%M:%S}", *parts, flush=True)


def relative_delta(value: int, encoding: str) -> int:
    """Signed step count from a relative encoder value."""
    if encoding == "twos":
        return value - 128 if value >= 64 else value
    return value - 64


class Executor:
    """Performs actions. With dry_run, only logs what it would do."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._kb = self._mouse = None

    def _keyboard(self):
        if self._kb is None:
            from pynput.keyboard import Controller

            self._kb = Controller()
        return self._kb

    def keys(self, combo: str, times: int = 1) -> None:
        if self.dry_run:
            log(f"  [dry-run] keys {combo}" + (f" x{times}" if times > 1 else ""))
            return
        from pynput.keyboard import Key

        mods, key = parse_combo(combo)
        kb = self._keyboard()
        target = getattr(Key, key) if len(key) > 1 else key
        with kb.pressed(*(getattr(Key, m) for m in mods)):
            for _ in range(times):
                kb.tap(target)

    def text(self, text: str) -> None:
        if self.dry_run:
            log(f"  [dry-run] type {text!r}")
            return
        from pynput.keyboard import Key

        kb = self._keyboard()
        for i, line in enumerate(text.split("\n")):
            if i:
                kb.tap(Key.enter)
            if line:
                kb.type(line)

    def shell(self, command: str) -> None:
        if self.dry_run:
            log(f"  [dry-run] shell {command!r}")
            return
        SHELL_LOG.parent.mkdir(parents=True, exist_ok=True)
        out = open(SHELL_LOG, "a")  # noqa: SIM115 - handed to the child process
        out.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} $ {command}\n")
        out.flush()
        subprocess.Popen(command, shell=True, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()

    def app(self, name: str) -> None:
        if self.dry_run:
            log(f"  [dry-run] open app {name!r}")
            return
        subprocess.Popen(
            app_command(name), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )

    def scroll(self, lines: int) -> None:
        if self.dry_run:
            log(f"  [dry-run] scroll {lines:+d}")
            return
        if self._mouse is None:
            from pynput.mouse import Controller

            self._mouse = Controller()
        self._mouse.scroll(0, -lines)  # clockwise = scroll down


# How far a jog wheel or knob turns (in ticks) per step, unless a mapping sets `ticks`. Turning a wheel sends a
# fast stream of ticks; one key combo per tick switches dozens of tabs per flick, so "turn" mappings need far
# more movement per step than scrolling does.
DEFAULT_TICKS = {"scroll": 1, "turn": 24}
IDLE_RESET = 0.6  # seconds; leftover movement from an earlier turn doesn't count towards the next step


class Mapper:
    def __init__(self, config: Config, executor: Executor):
        self.config = config
        self.executor = executor
        self._travel: dict[Control, tuple[int, float]] = {}  # accumulated ticks, time of last tick

    def handle(self, msg) -> Mapping | None:
        ctrl = Control.from_message(msg)
        m = find_mapping(self.config.mappings, ctrl) if ctrl else None
        if m is None:
            return None
        ex = self.executor
        if m.scroll is not None or m.keys_cw is not None:
            delta = relative_delta(msg.value, m.encoding)
            if delta == 0:
                return None
            per_step = m.ticks or DEFAULT_TICKS["scroll" if m.scroll is not None else "turn"]
            now = time.monotonic()
            travel, last = self._travel.get(ctrl, (0, 0.0))
            if now - last > IDLE_RESET or (travel > 0) != (delta > 0):
                travel = 0  # a fresh turn, or a change of direction, starts counting again
            travel += delta
            steps = min(abs(travel) // per_step, MAX_STEPS_PER_MESSAGE)
            travel -= steps * per_step * (1 if travel > 0 else -1)
            self._travel[ctrl] = (travel, now)
            if steps == 0:
                return None
            if m.scroll is not None:
                ex.scroll(steps * m.scroll * (1 if delta > 0 else -1))
            else:
                ex.keys(m.keys_cw if delta > 0 else m.keys_ccw, times=steps)
            return m
        if ctrl.kind == "cc" and msg.value != 127:
            return None  # a cc button/fader fires only at full value
        if m.keys:
            ex.keys(m.keys)
        elif m.text is not None:
            ex.text(m.text)
        elif m.shell:
            ex.shell(m.shell)
        elif m.app:
            ex.app(m.app)
        return m


def _proc_name(name: str) -> str:
    """Case-insensitive process name without a Windows .exe suffix."""
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


class PauseWatcher:
    """Tracks whether any of the given apps is running (checked every `interval` seconds)."""

    def __init__(self, process_names: list[str], interval: float = 2.0, on_change=None):
        self.names = {_proc_name(n) for n in process_names}
        self.interval = interval
        self.on_change = on_change
        self.running_app: str | None = None
        if self.names:
            self._check()
            threading.Thread(target=self._loop, daemon=True).start()

    def _check(self) -> None:
        import psutil

        found = None
        for p in psutil.process_iter(["name"]):
            if _proc_name(p.info.get("name") or "") in self.names:
                found = p.info["name"]
                break
        if found != self.running_app:
            log(f"paused: {found} is running" if found else "resumed")
            self.running_app = found
            if self.on_change:
                self.on_change()

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self._check()
            except Exception as e:  # noqa: BLE001 - never let the watcher thread die
                log("pause check failed:", e)

    @property
    def paused(self) -> bool:
        return self.running_app is not None


def accessibility_trusted() -> bool | None:
    """Whether macOS lets this process send keystrokes. None if not on macOS or it can't be determined."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return None


def input_names() -> list[str]:
    """Connected MIDI inputs; empty if the system has no MIDI support (e.g. Linux without ALSA)."""
    import mido

    from . import midiwatch

    midiwatch.start()
    try:
        return [n for n in mido.get_input_names() if n]  # a device that just vanished can show up nameless
    except Exception:  # noqa: BLE001 - rtmidi raises its own error types when no MIDI backend is available
        return []


def find_port(device: str) -> str:
    names = input_names()
    for n in names:
        if device.lower() in n.lower():
            return n
    raise SystemExit(f"No MIDI input matching {device!r}. Connected: {names or 'none'}")

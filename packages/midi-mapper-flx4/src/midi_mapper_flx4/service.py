"""The background mapper process, and capturing a single button press for the MCP server.

Files next to the config:
  mapper.pid   pid of the running mapper (`run` or `gui`), so only one mapper drives the controller
  mapper.log   output of a mapper started in the background
  learning     while it exists (and holds a future Unix time), the mapper ignores presses, so pressing
               a button to map it doesn't also trigger whatever it currently does
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import Control
from .flx4 import is_jog

SETTLE_SECONDS = 0.6  # after the first message, how long to keep listening to tell a jog turn from a touch


def pid_file(config: Path) -> Path:
    return Path(config).parent / "mapper.pid"


def log_file(config: Path) -> Path:
    return Path(config).parent / "mapper.log"


def learn_file(config: Path) -> Path:
    return Path(config).parent / "learning"


def _alive(pid: int) -> bool:
    import psutil

    try:
        cmd = " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return "midi-mapper-flx4" in cmd or "midi_mapper_flx4" in cmd


def running_pid(config: Path) -> int | None:
    """Pid of the mapper using this config, if one is running."""
    try:
        pid = int(pid_file(config).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and _alive(pid) else None


def claim(config: Path) -> None:
    """Record this process as the mapper. Raises if another mapper already runs."""
    other = running_pid(config)
    if other:
        raise RuntimeError(
            f"another mapper is already running (pid {other}). Stop it with `midi-mapper-flx4 stop` first."
        )
    pid_file(config).write_text(str(os.getpid()))


def release(config: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        if int(pid_file(config).read_text()) == os.getpid():
            pid_file(config).unlink()


def start_background(config: Path, wait: float = 5.0) -> int:
    """Start `midi-mapper-flx4 run` detached from this process; returns its pid."""
    pid = running_pid(config)
    if pid:
        return pid
    out = open(log_file(config), "a")  # noqa: SIM115 - handed to the child process
    proc = subprocess.Popen(
        [sys.executable, "-m", "midi_mapper_flx4.cli", "--config", str(config), "run"],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    out.close()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if running_pid(config) == proc.pid:
            return proc.pid
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    tail = log_file(config).read_text().strip().splitlines()[-3:]
    raise RuntimeError("the mapper didn't start: " + (" / ".join(tail) or "no output"))


def stop_background(config: Path, wait: float = 5.0) -> int | None:
    pid = running_pid(config)
    if not pid:
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and running_pid(config):
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        pid_file(config).unlink()
    return pid


def learning_active(config: Path) -> bool:
    try:
        return float(learn_file(config).read_text()) > time.time()
    except (OSError, ValueError):
        return False


def pick_control(seen: list[Control]) -> Control:
    """The control the user meant, from everything the controller sent in the settle window.

    Turning a jog wheel by its top first sends a touch note, so a jog turn wins over the touch. Faders and
    knobs send 14-bit values as two CCs (n and n + 32); the second one is dropped.
    """
    jog = next((c for c in seen if is_jog(c)), None)
    if jog:
        return jog
    for c in seen:
        if c.kind == "cc" and c.number >= 32 and Control("cc", c.channel, c.number - 32) in seen:
            continue
        return c
    return seen[0]


def capture(port_name: str, config: Path, timeout: float) -> Control | None:
    """Wait for the user to press a control. The running mapper ignores presses meanwhile."""
    import mido

    flag = learn_file(config)
    flag.write_text(str(time.time() + timeout + SETTLE_SECONDS + 5))
    try:
        with mido.open_input(port_name) as port:
            for _ in port.iter_pending():
                pass
            seen: list[Control] = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                msg = port.poll()
                if msg is None:
                    time.sleep(0.005)
                    continue
                ctrl = Control.from_message(msg)
                if ctrl is None:
                    continue
                if not seen:
                    deadline = time.monotonic() + SETTLE_SECONDS
                if ctrl not in seen:
                    seen.append(ctrl)
            return pick_control(seen) if seen else None
    finally:
        # Releases still arriving would hit the mapper; they're note-offs, which never fire anything.
        with contextlib.suppress(OSError):
            flag.unlink()

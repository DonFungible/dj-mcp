"""Interactive terminal mapper: a drawing of the FLX4 that lights up the control you press, then asks what it
should do.

    midi-mapper-flx4 map
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from . import service
from .config import ConfigError, Control, Mapping, load_config, mapping_from_json, mapping_to_json
from .diagram import WIDTH, render
from .engine import Executor, Mapper, input_names
from .flx4 import DEVICE, control_id, control_name, is_jog

CLEAR, HIDE_CURSOR, SHOW_CURSOR = "\x1b[H\x1b[2J", "\x1b[?25l", "\x1b[?25h"
BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"
SETTLE = 0.35  # seconds to keep listening after the first message, to tell a jog turn from a touch


class Keys:
    """Single keypresses without Enter, plus a way back to normal line input for prompts."""

    def __enter__(self) -> Keys:
        if os.name == "nt":
            self._fd = None
            return self
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        self._restore()

    def _restore(self) -> None:
        if self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def read(self, timeout: float) -> str | None:
        if os.name == "nt":
            import msvcrt

            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.01)
            return None
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return os.read(self._fd, 1).decode(errors="ignore") if ready else None

    @contextlib.contextmanager
    def lines(self) -> Iterator[None]:
        """Normal, echoed line input for the duration of a prompt."""
        self._restore()
        sys.stdout.write(SHOW_CURSOR)
        try:
            yield
        finally:
            if self._fd is not None:
                import tty

                tty.setcbreak(self._fd)
            sys.stdout.write(HIDE_CURSOR)


def describe(m: Mapping) -> str:
    a = mapping_to_json(m)["action"]
    if a["type"] == "turn":
        return f"turn: {a['cw']} clockwise, {a['ccw']} counter-clockwise"
    what = {"app": "open", "keys": "press", "text": "type", "shell": "run", "scroll": "scroll"}[a["type"]]
    value = repr(a["value"]) if a["type"] == "text" else a["value"]
    suffix = " lines per step" if a["type"] == "scroll" else ""
    return f"{what} {value}{suffix}" + (f'  ("{m.name}")' if m.name else "")


class MapperUI:
    def __init__(self, config: Path):
        self.config = Path(config)
        self.selected: Control | None = None
        self.message = "Press a button, pad or knob on your FLX4."
        self.color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    # ---------------------------------------------------------------- state
    def mappings(self) -> dict[Control, Mapping]:
        try:
            return load_config(self.config).mappings
        except (OSError, ConfigError):
            return {}

    def save(self, spec: dict, name: str) -> None:
        from .runtime import Runtime

        c = self.selected
        assert c is not None
        m = mapping_from_json(
            {"control": {"kind": c.kind, "channel": c.channel, "number": c.number}, "action": spec, "name": name}
        )
        Runtime(self.config, verbose=False).save_mapping(m)
        self.message = f"Saved: {control_name(c)} → {describe(m)}"

    def remove(self) -> None:
        from .runtime import Runtime

        c = self.selected
        if c is None or c not in self.mappings():
            self.message = "That control isn't mapped."
            return
        Runtime(self.config, verbose=False).delete_mapping(c)
        self.message = f"Removed the mapping for {control_name(c)}."

    # -------------------------------------------------------------- drawing
    def draw(self) -> None:
        maps = self.mappings()
        out = [CLEAR, render({control_id(c) for c in maps}, self.selected, color=self.color), ""]
        if self.selected:
            c = self.selected
            current = maps.get(c)
            out.append(f"{BOLD}{control_name(c)}{RESET}  {DIM}{control_id(c)}{RESET}")
            out.append(f"  does: {describe(current) if current else 'nothing yet'}")
            if is_jog(c):
                menu = "[s] scroll   [k] keys per step"
            else:
                menu = "[a] open app   [k] key combo   [t] type text   [c] shell command"
            extra = "   [x] remove   [r] try it" if current else ""
            out.append(f"  {menu}{extra}   [q] quit")
        else:
            out += ["", "", "  [q] quit"]
        out.append("")
        out.append(f"  {self.message}")
        if not self.color:
            out[0] = ""
        sys.stdout.write(HIDE_CURSOR + "\n".join(out) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------- prompts
    def ask(self, keys: Keys, choice: str) -> None:
        c = self.selected
        assert c is not None
        jog = is_jog(c)
        prompts = {
            "a": ("Application to open (e.g. Discord): ", "app"),
            "k": ("Key combo (e.g. cmd+shift+p): ", "keys"),
            "t": ("Text to type (\\n presses Enter): ", "text"),
            "c": ("Shell command: ", "shell"),
            "s": ("Lines to scroll per step (negative reverses): ", "scroll"),
        }
        if choice == "x":
            self.remove()
            return
        if choice == "r":
            self.try_it()
            return
        allowed = {"s", "k"} if jog else {"a", "k", "t", "c"}
        if choice not in allowed:
            return
        with keys.lines():
            try:
                if jog and choice == "k":
                    cw = input("  Combo per clockwise step (e.g. down): ").strip()
                    ccw = input("  Combo per counter-clockwise step (e.g. up): ").strip()
                    spec = {"type": "turn", "cw": cw, "ccw": ccw}
                else:
                    question, kind = prompts[choice]
                    value = input("  " + question)
                    if kind == "text":
                        value = value.replace("\\n", "\n")
                    elif not value.strip():
                        self.message = "Nothing entered; not saved."
                        return
                    spec = {"type": kind, "value": value.strip() if kind != "text" else value}
                default = f"Open {spec['value']}" if spec["type"] == "app" else ""
                hint = f" (Enter for '{default}')" if default else " (optional)"
                name = input(f"  Name{hint}: ").strip() or default
            except (EOFError, KeyboardInterrupt):
                self.message = "Cancelled."
                return
        try:
            self.save(spec, name)
        except ConfigError as e:
            self.message = f"Not saved: {e}"

    def try_it(self) -> None:
        import mido

        c = self.selected
        maps = self.mappings()
        if c is None or c not in maps:
            self.message = "Map this control first."
            return
        if is_jog(c) or c.kind == "cc" and maps[c].scroll is not None:
            self.message = "Turn the jog wheel to try it."
            return
        if c.kind == "note":
            msg = mido.Message("note_on", channel=c.channel - 1, note=c.number, velocity=127)
        else:
            msg = mido.Message("control_change", channel=c.channel - 1, control=c.number, value=127)
        Mapper(load_config(self.config), Executor()).handle(msg)
        self.message = f"Ran: {describe(maps[c])}"

    # ----------------------------------------------------------------- loop
    def run(self) -> None:
        import mido

        device = load_config(self.config).device
        port_name = next((n for n in input_names() if device.lower() in n.lower()), None)
        if port_name is None:
            raise SystemExit(f"The {DEVICE} isn't connected. Plug it in over USB and try again.")
        if shutil.get_terminal_size().columns < WIDTH + 2:
            print(f"Tip: widen the terminal to {WIDTH + 2} columns to see the whole controller.")
        # Windows lets only one program open a MIDI device, so the background mapper steps aside meanwhile.
        paused = os.name == "nt" and service.stop_background(self.config)
        flag = service.learn_file(self.config)
        try:
            with mido.open_input(port_name) as port, Keys() as keys:
                self.draw()
                seen: list[Control] = []
                settle_until = 0.0
                last_flag = 0.0
                while True:
                    now = time.monotonic()
                    if now - last_flag > 2:  # keep the running mapper from firing what you press
                        flag.write_text(str(time.time() + 10))
                        last_flag = now
                    for msg in port.iter_pending():
                        ctrl = Control.from_message(msg)
                        if ctrl and ctrl not in seen:
                            if not seen:
                                settle_until = now + SETTLE
                            seen.append(ctrl)
                    if seen and now >= settle_until:
                        self.selected = service.pick_control(seen)
                        self.message = "Choose what it should do, or press another control."
                        seen = []
                        self.draw()
                    key = keys.read(0.02)
                    if key is None:
                        continue
                    key = key.lower()
                    if key in ("q", "\x1b", "\x03"):
                        break
                    if self.selected:
                        self.ask(keys, key)
                        for _ in port.iter_pending():  # ignore anything pressed while typing
                            pass
                        self.draw()
        finally:
            with contextlib.suppress(OSError):
                flag.unlink()
            sys.stdout.write(SHOW_CURSOR)
            if paused:
                service.start_background(self.config)
        self.offer_mapper()

    def offer_mapper(self) -> None:
        if not self.mappings() or service.running_pid(self.config):
            return
        try:
            answer = input("\nThe mapper isn't running, so these mappings won't do anything yet. Start it? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            return
        if answer.strip().lower() in ("", "y", "yes"):
            pid = service.start_background(self.config)
            print(f"Mapper running in the background (pid {pid}). Stop it with `midi-mapper-flx4 stop`.")


def run(config: Path) -> None:
    MapperUI(config).run()

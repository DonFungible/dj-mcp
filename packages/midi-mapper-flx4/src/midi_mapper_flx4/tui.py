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

CLEAR, HOME, HIDE_CURSOR, SHOW_CURSOR = "\x1b[H\x1b[2J", "\x1b[H", "\x1b[?25l", "\x1b[?25h"
CLEAR_LINE, CLEAR_BELOW = "\x1b[K", "\x1b[J"
BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"
SETTLE = 0.35  # after the first message, keep refining the selection this long (a jog touch may become a turn)


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

    def flush(self) -> None:
        """Drop keystrokes that reached the terminal while recording a combo."""
        if self._fd is not None:
            import termios

            termios.tcflush(self._fd, termios.TCIFLUSH)

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
        self._cache: tuple[int, dict[Control, Mapping]] | None = None
        self._drawn = False

    # ---------------------------------------------------------------- state
    def mappings(self) -> dict[Control, Mapping]:
        """Current mappings, re-read only when the config file changes."""
        try:
            mtime = self.config.stat().st_mtime_ns
            if self._cache is None or self._cache[0] != mtime:
                self._cache = (mtime, load_config(self.config).mappings)
            return self._cache[1]
        except (OSError, ConfigError):
            return {}

    def save(self, spec: dict, name: str) -> None:
        from .runtime import Runtime

        c = self.selected
        assert c is not None
        ticks = spec.pop("ticks", None)
        m = mapping_from_json(
            {
                "control": {"kind": c.kind, "channel": c.channel, "number": c.number},
                "action": spec,
                "name": name,
                "ticks": ticks,
            }
        )
        Runtime(self.config, verbose=False).save_mapping(m)
        self.message = f"Saved: {control_name(c)} → {describe(m)}"
        if service.needs_keystrokes(m) and self.keystrokes_blocked():
            self.ask_for_keystrokes()
            self.message += f"\n  {service.PROMPTED}"

    def keystrokes_blocked(self) -> bool:
        from . import permissions

        if service.running_pid(self.config):
            return service.mapper_can_type(self.config) is False
        return permissions.can_send_keys() is False

    def ask_for_keystrokes(self) -> None:
        """Have macOS prompt for Accessibility for this terminal (the mapper gets it too: same terminal)."""
        from . import permissions

        permissions.request_send_keys()

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
        out = [render({control_id(c) for c in maps}, self.selected, color=self.color), ""]
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
        out.append("")
        if not service.running_pid(self.config):
            out.append(
                f"  {DIM}The mapper isn't running, so mappings do nothing yet. It's offered when you quit.{RESET}"
            )
        else:
            out.append(f"  {DIM}Your mappings are paused while this screen is open; quit (q) to use them.{RESET}")
            if service.mapper_can_type(self.config) is False:
                out.append("  ! " + service.KEYSTROKE_HELP)
        if not self.color:
            sys.stdout.write("\n".join(out) + "\n")
        else:
            # Repaint in place (no full clear) so the terminal only redraws what changed.
            start = HOME if self._drawn else CLEAR
            sys.stdout.write(HIDE_CURSOR + start + "\n".join(line + CLEAR_LINE for line in out) + "\n" + CLEAR_BELOW)
        self._drawn = True
        sys.stdout.flush()

    # ------------------------------------------------------------- prompts
    def ask(self, keys: Keys, choice: str) -> None:
        c = self.selected
        assert c is not None
        jog = is_jog(c)
        prompts = {
            "a": ("Application to open (e.g. Discord): ", "app"),
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
                    cw = self.combo(keys, "clockwise step (e.g. cmd+shift+] for the next tab)")
                    ccw = self.combo(keys, "counter-clockwise step (e.g. cmd+shift+[)")
                    if not cw or not ccw:
                        self.message = "Nothing entered; not saved."
                        return
                    spec = {"type": "turn", "cw": cw, "ccw": ccw}
                    ticks = input("  How far to turn per step? Enter for 24; higher is less sensitive: ").strip()
                    if ticks:
                        spec["ticks"] = ticks
                elif choice == "k":
                    value = self.combo(keys, "key combo (e.g. cmd+shift+p)")
                    if not value:
                        self.message = "Nothing entered; not saved."
                        return
                    spec = {"type": "keys", "value": value}
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

    def combo(self, keys: Keys, what: str) -> str:
        """Record a combo by pressing it; Esc, or no keyboard access, falls back to typing it."""
        from .keycapture import RecordError, record_combo

        print(f"\n  Press the {what} now. Esc to type it instead.", flush=True)
        try:
            pressed = record_combo()
        except RecordError as e:
            print(f"  {e}")
            pressed = None
        keys.flush()
        if pressed:
            print(f"  Recorded: {pressed}")
            return pressed
        return input(f"  Type the {what}: ").strip()

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
        if any(service.needs_keystrokes(m) for m in self.mappings().values()) and self.keystrokes_blocked():
            self.ask_for_keystrokes()
            self.message = service.PROMPTED
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
                    changed = False
                    for msg in port.iter_pending():
                        ctrl = Control.from_message(msg)
                        if ctrl and ctrl not in seen:
                            if not seen:
                                settle_until = now + SETTLE
                            seen.append(ctrl)
                            changed = True
                    if changed:  # light up right away; later messages in the window can refine the choice
                        pick = service.pick_control(seen)
                        if pick != self.selected:
                            self.selected = pick
                            self.message = "Choose what it should do, or press another control."
                            self.draw()
                    if seen and now >= settle_until:
                        seen = []
                    key = keys.read(0.005)
                    if key is None:
                        continue
                    key = key.lower()
                    if key in ("q", "\x1b", "\x03"):
                        break
                    if self.selected:
                        self.ask(keys, key)
                        for _ in port.iter_pending():  # ignore anything pressed while typing
                            pass
                        self._drawn = False  # the prompt scrolled the screen; start clean
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

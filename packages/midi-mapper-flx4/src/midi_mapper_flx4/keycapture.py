"""Record a key combo by pressing it, instead of typing "cmd+shift+]".

Terminals never receive Cmd shortcuts (macOS gives them to the terminal app itself), so this listens to the
keyboard system-wide with pynput. On macOS the keys are swallowed while recording, so pressing Cmd+Shift+]
doesn't also switch your terminal tab. macOS asks for Accessibility (and Input Monitoring) permission for the
app running this; without it no keys arrive and the caller falls back to typing.
"""

from __future__ import annotations

import sys
import threading

MODIFIERS = {"cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd", "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
             "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "shift": "shift", "shift_l": "shift",
             "shift_r": "shift"}  # fmt: skip
SPECIAL = {"enter": "enter", "esc": "esc", "tab": "tab", "space": "space", "backspace": "backspace",
           "delete": "delete", "up": "up", "down": "down", "left": "left", "right": "right", "home": "home",
           "end": "end", "page_up": "pageup", "page_down": "pagedown"}  # fmt: skip
SPECIAL.update({f"f{i}": f"f{i}" for i in range(1, 21)})
ORDER = ["cmd", "ctrl", "alt", "shift"]

# macOS virtual key codes (ANSI layout) -> the unshifted character, so Shift+] records as "]" not "}".
MAC_VK = {
    0x00: "a", 0x01: "s", 0x02: "d", 0x03: "f", 0x04: "h", 0x05: "g", 0x06: "z", 0x07: "x", 0x08: "c",
    0x09: "v", 0x0B: "b", 0x0C: "q", 0x0D: "w", 0x0E: "e", 0x0F: "r", 0x10: "y", 0x11: "t", 0x12: "1",
    0x13: "2", 0x14: "3", 0x15: "4", 0x16: "6", 0x17: "5", 0x18: "=", 0x19: "9", 0x1A: "7", 0x1B: "-",
    0x1C: "8", 0x1D: "0", 0x1E: "]", 0x1F: "o", 0x20: "u", 0x21: "[", 0x22: "i", 0x23: "p", 0x25: "l",
    0x26: "j", 0x27: "'", 0x28: "k", 0x29: ";", 0x2A: "\\", 0x2B: ",", 0x2C: "/", 0x2D: "n", 0x2E: "m",
    0x2F: ".", 0x32: "`",
}  # fmt: skip
# Shifted characters back to their key, for platforms where only the typed character is known.
UNSHIFT = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./", strict=True))


class RecordError(RuntimeError):
    pass


def key_name(key) -> str | None:
    """Our combo name for a pynput key ("cmd", "enter", "]"), or None if it isn't usable."""
    name = getattr(key, "name", None)  # pynput.keyboard.Key members
    if name:
        return MODIFIERS.get(name) or SPECIAL.get(name)
    vk = getattr(key, "vk", None)
    if sys.platform == "darwin" and vk in MAC_VK:
        return MAC_VK[vk]
    char = getattr(key, "char", None)
    if not char or len(char) != 1 or not char.isprintable():
        return None
    char = UNSHIFT.get(char, char).lower()
    return "plus" if char == "+" else char


def combo(modifiers: set[str], key: str) -> str:
    return "+".join([m for m in ORDER if m in modifiers] + [key])


def record_combo(timeout: float = 20.0) -> str | None:
    """Wait for one key combo and return it ("cmd+shift+]"). Esc alone cancels (returns None)."""
    import logging

    logging.getLogger("pynput").setLevel(logging.ERROR)  # we explain missing permissions ourselves
    try:
        from pynput import keyboard
    except Exception as e:  # noqa: BLE001 - pynput can fail to load without a display (Linux)
        raise RecordError(f"keyboard recording isn't available here: {e}") from e

    held: set[str] = set()
    result: list[str | None] = []
    done = threading.Event()

    def on_press(key):
        name = key_name(key)
        if name is None:
            return None
        if name in ORDER:
            held.add(name)
            return None
        result.append(None if name == "esc" and not held else combo(held, name))
        done.set()
        return False  # stop listening

    def on_release(key):
        held.discard(key_name(key) or "")

    kwargs = {}
    if sys.platform == "darwin":
        # Swallow every key while recording so the combo doesn't also act on the terminal or the front app.
        kwargs["darwin_intercept"] = lambda event_type, event: None
    listener = keyboard.Listener(on_press=on_press, on_release=on_release, **kwargs)
    listener.start()
    try:
        listener.wait()
        if sys.platform == "darwin" and getattr(listener, "IS_TRUSTED", True) is False:
            raise RecordError(
                "macOS didn't allow listening to the keyboard. Allow your terminal in System Settings > Privacy & "
                "Security > Accessibility and Input Monitoring, then restart it."
            )
        if not done.wait(timeout):
            raise RecordError("No keys were pressed.")
        return result[0]
    finally:
        listener.stop()

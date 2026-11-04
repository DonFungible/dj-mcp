"""Serato DJ Pro application control on macOS: launch, quit, focus, and keyboard-driven library search and
track loading via System Events. Keyboard control needs Accessibility permission for the app running the MCP
client (System Settings > Privacy & Security > Accessibility)."""

from __future__ import annotations

import asyncio
import os
import re

from .writer import serato_running

APP_NAME = os.environ.get("SERATO_APP_NAME") or "Serato DJ Pro"


async def _run(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"{args[0]} timed out") from None
    return proc.returncode or 0, out.decode(), err.decode()


async def osascript(script: str) -> str:
    code, out, err = await _run("osascript", "-e", script)
    if code != 0:
        if re.search(r"not allowed|assistive|1002|-25211|-1719", err, re.I):
            raise PermissionError(
                "macOS blocked keyboard control. Give the app running this MCP client (Terminal, Claude, Cursor...) "
                "Accessibility access in System Settings > Privacy & Security > Accessibility, then retry."
            )
        raise RuntimeError(f"AppleScript failed: {err.strip()}")
    return out.strip()


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def launch() -> str:
    if serato_running():
        return f"{APP_NAME} is already running"
    await _run("open", "-a", APP_NAME)
    return f"Launching {APP_NAME}"


async def quit_app() -> str:
    if not serato_running():
        return f"{APP_NAME} is not running"
    await osascript(f'tell application "{_esc(APP_NAME)}" to quit')
    for _ in range(40):
        await asyncio.sleep(0.5)
        if not serato_running():
            return f"{APP_NAME} quit"
    return f"Asked {APP_NAME} to quit, but it is still running (it may be showing a dialog)"


async def focus() -> None:
    if not serato_running():
        raise RuntimeError(f"{APP_NAME} is not running. Use launch_serato first.")
    await osascript(f'tell application "{_esc(APP_NAME)}" to activate')


KEY_CODES = {
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "return": 36,
    "enter": 76,
    "tab": 48,
    "escape": 53,
    "space": 49,
    "delete": 51,
}


async def keystroke(key: str, modifiers: list[str] | None = None) -> None:
    """Send one keystroke to Serato, e.g. key "f" with ["command"], or "left" with ["shift"]."""
    await focus()
    using = f" using {{{', '.join(f'{m} down' for m in modifiers)}}}" if modifiers else ""
    code = KEY_CODES.get(key.lower())
    action = f"key code {code}" if code is not None else f'keystroke "{_esc(key)}"'
    await osascript(f'tell application "System Events" to tell process "{_esc(APP_NAME)}" to {action}{using}')


async def search(query: str) -> None:
    """Type a query into Serato's library search box (Cmd+F) and leave focus on the results."""
    await focus()
    await osascript(
        f"""
    tell application "System Events" to tell process "{_esc(APP_NAME)}"
      keystroke "f" using {{command down}}
      delay 0.15
      keystroke "a" using {{command down}}
      key code 51
      keystroke "{_esc(query)}"
      delay 0.6
      key code 36
    end tell"""
    )


async def load_selected(deck: int) -> None:
    """Load the highlighted track to the left (deck 1) or right (deck 2) deck with Shift+Left / Shift+Right."""
    await keystroke("left" if deck == 1 else "right", ["shift"])

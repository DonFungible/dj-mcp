"""macOS privacy permissions: check them, and ask macOS to show its own permission prompt when one is needed.

Two permissions matter, both granted to the app that started midi-mapper-flx4 (usually your terminal):
  Accessibility     sending keystrokes and scrolling
  Input Monitoring  recording a shortcut by pressing it

macOS shows each prompt once per app. After that (for example if it was dismissed), asking again opens the
right page of System Settings instead. On other systems everything here reports None and does nothing.
"""

from __future__ import annotations

import subprocess
import sys

SETTINGS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}

_asked: set[str] = set()  # prompts already shown by this process


def _mac() -> bool:
    return sys.platform == "darwin"


def can_send_keys() -> bool | None:
    """Accessibility: may this process send keystrokes and scroll?"""
    if not _mac():
        return None
    try:
        import HIServices

        return bool(HIServices.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - pyobjc missing or broken
        return None


def can_listen() -> bool | None:
    """Input Monitoring: may this process read the keyboard?"""
    if not _mac():
        return None
    try:
        import Quartz

        return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:  # noqa: BLE001
        return None


def open_settings(which: str) -> None:
    if _mac():
        subprocess.Popen(["open", SETTINGS[which]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def request_send_keys() -> bool | None:
    """Make sure keystrokes are allowed, prompting the first time and opening Settings after that."""
    if not _mac() or can_send_keys():
        return can_send_keys()
    try:
        import HIServices

        if "accessibility" not in _asked:
            _asked.add("accessibility")
            HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
        else:
            open_settings("accessibility")
    except Exception:  # noqa: BLE001
        open_settings("accessibility")
    return can_send_keys()


def request_listen() -> bool | None:
    """Make sure the keyboard can be read (Input Monitoring), prompting or opening Settings as needed."""
    if not _mac() or can_listen():
        return can_listen()
    try:
        import Quartz

        if "input_monitoring" not in _asked:
            _asked.add("input_monitoring")
            Quartz.CGRequestListenEventAccess()
        else:
            open_settings("input_monitoring")
    except Exception:  # noqa: BLE001
        open_settings("input_monitoring")
    return can_listen()

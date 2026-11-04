"""MCP server: map DDJ-FLX4 buttons by talking to an AI assistant.

The flow it is built for: the user says "map a button to open Discord", the assistant tells them to
press it and calls wait_for_button, then saves the mapping with map_button. Mappings are carried out by
the mapper, a separate background process that keeps running after the assistant closes.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import service
from .config import DEFAULT_CONFIG, Config, ConfigError, Control, load_config, mapping_from_json, mapping_to_json
from .engine import Executor, Mapper, accessibility_trusted, input_names
from .flx4 import DEVICE, control_id, control_name, is_jog, parse_control_id
from .runtime import Runtime

mcp = MCPServer(
    "midi-mapper-flx4",
    instructions=(
        "Maps buttons, pads and jog wheels on the user's Pioneer DDJ-FLX4 DJ controller to computer actions. "
        "To map a button: first tell the user to press the button on their FLX4 (the call waits for it), then "
        "call wait_for_button, then map_button with the returned control id. Turn the request into the "
        "simplest action: 'open Discord' -> app 'Discord'; a shortcut -> keys like 'cmd+shift+m'; typing -> "
        "text; anything else -> a shell command. Prefer app and keys over shell. Don't map a shell command the "
        "user didn't ask for, and say what a shell command does before mapping it. Mappings only work while the "
        "mapper runs; map_button starts it. It pauses itself while rekordbox or Serato is open, so the FLX4 "
        "still works for DJing."
    ),
)

config_path: Path = DEFAULT_CONFIG

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
ACTS = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

ActionType = Literal["app", "keys", "text", "shell", "scroll", "turn"]


def tool(annotations: ToolAnnotations) -> Callable[[Callable], Callable]:
    """Register a tool, reporting expected failures to the model instead of a generic error."""

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ConfigError, RuntimeError, OSError) as e:
                raise ToolError(str(e)) from e

        return mcp.tool(annotations=annotations)(wrapper)

    return decorate


def _config() -> Config:
    return load_config(config_path)


def _port() -> str:
    names = input_names()
    device = _config().device
    port = next((n for n in names if device.lower() in n.lower()), None)
    if port is None:
        raise RuntimeError(f"The {DEVICE} isn't connected. Plug it in over USB and try again.")
    return port


def _describe(m) -> dict[str, Any]:
    return {**mapping_to_json(m), "id": control_id(m.control), "control_name": control_name(m.control)}


def _editor() -> Runtime:
    """A Runtime used only to edit the config file (it is never started, so it doesn't touch MIDI)."""
    return Runtime(config_path, verbose=False)


def _ensure_mapper() -> dict[str, Any]:
    was_running = service.running_pid(config_path)
    pid = service.start_background(config_path)
    return {"mapper_pid": pid, "mapper_started": not was_running}


# ------------------------------------------------------------------------ tools
@tool(READ)
def status() -> dict[str, Any]:
    """Whether the FLX4 is connected, the mapper is running, and keystrokes are allowed; plus all mappings."""
    cfg = _config()
    pid = service.running_pid(config_path)
    trusted = accessibility_trusted()
    return {
        "controller": DEVICE,
        "connected": any(cfg.device.lower() in n.lower() for n in input_names()),
        "mapper_running": pid is not None,
        "mapper_pid": pid,
        "accessibility": trusted,
        "accessibility_note": (
            "macOS is blocking keystrokes and scrolling: the user must allow their terminal (or the app running "
            "this) in System Settings > Privacy & Security > Accessibility, then restart the mapper. "
            "App and shell actions work without it."
            if trusted is False
            else None
        ),
        "paused_while_running": cfg.pause_when_running,
        "config_path": str(config_path),
        "mappings": [_describe(m) for m in cfg.mappings.values()],
    }


@tool(READ)
def wait_for_button(timeout_seconds: int = 60) -> dict[str, Any]:
    """Wait for the user to press a button or pad, or turn a jog wheel, on their FLX4, and identify it.

    Tell the user to press the button BEFORE calling this; it blocks until they do (or the timeout).
    While waiting, the mapper ignores presses, so the button's current action doesn't fire.
    Returns the control id to pass to map_button, its name (e.g. "Deck 1 Play/Pause"), and its current
    mapping if it has one. Shift + a button counts as a separate button.
    """
    timeout = max(5, min(int(timeout_seconds), 300))
    port = _port()
    # Windows lets only one program open a MIDI device, so the mapper steps aside while we listen.
    paused = sys.platform == "win32" and service.stop_background(config_path)
    try:
        ctrl = service.capture(port, config_path, timeout)
    finally:
        if paused:
            service.start_background(config_path)
    if ctrl is None:
        raise ToolError(f"No button was pressed within {timeout} seconds.")
    current = _config().mappings.get(ctrl)
    return {
        "id": control_id(ctrl),
        "control_name": control_name(ctrl),
        "kind": "jog wheel" if is_jog(ctrl) else ("knob/fader" if ctrl.kind == "cc" else "button/pad"),
        "suitable_actions": ["scroll", "turn"] if is_jog(ctrl) else ["app", "keys", "text", "shell"],
        "current_mapping": _describe(current) if current else None,
    }


@tool(WRITE)
def map_button(
    control: str,
    action: ActionType,
    value: str | None = None,
    counter_clockwise: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Save what a control does, replacing any existing mapping for it, and make sure the mapper is running.

    control: id from wait_for_button, e.g. "note:1:11".
    action and value:
      app    value = application name, e.g. "Discord". Opens it, or brings it to the front.
      keys   value = key combo, e.g. "cmd+shift+p". Modifiers: cmd, ctrl, alt/option, shift. Keys: letters,
             digits, punctuation, enter, esc, tab, space, backspace, delete, up/down/left/right, home, end,
             pageup, pagedown, f1-f20, plus.
      text   value = text to type; "\\n" presses Enter.
      shell  value = shell command, run in the background (output in mapper's shell.log).
      scroll value = lines per step, e.g. "3" (jog wheels only; negative reverses).
      turn   value = combo per clockwise step, counter_clockwise = combo per counter-clockwise step
             (jog wheels only), e.g. value "down", counter_clockwise "up".
    name: short label shown in lists, e.g. "Open Discord".
    """
    ctrl = parse_control_id(control)
    if action == "turn":
        spec = {"type": "turn", "cw": value, "ccw": counter_clockwise}
    else:
        spec = {"type": action, "value": value}
    m = mapping_from_json(
        {"control": {"kind": ctrl.kind, "channel": ctrl.channel, "number": ctrl.number}, "action": spec, "name": name}
    )
    replaced = _config().mappings.get(ctrl)
    _editor().save_mapping(m)
    result = {"saved": _describe(m), "replaced": _describe(replaced) if replaced else None, **_ensure_mapper()}
    if action in ("keys", "text", "scroll", "turn") and accessibility_trusted() is False:
        result["warning"] = (
            "macOS Accessibility permission is missing, so this mapping won't do anything yet. Allow the terminal "
            "in System Settings > Privacy & Security > Accessibility, then restart the mapper."
        )
    return result


@tool(DESTRUCTIVE)
def unmap_button(control: str) -> dict[str, Any]:
    """Remove a control's mapping. control: id like "note:1:11" (from status or wait_for_button)."""
    ctrl = parse_control_id(control)
    current = _config().mappings.get(ctrl)
    if current is None:
        raise ToolError(f"{control_name(ctrl)} isn't mapped.")
    _editor().delete_mapping(ctrl)
    return {"removed": _describe(current)}


@tool(ACTS)
def test_mapping(control: str) -> dict[str, Any]:
    """Carry out a button's mapped action once, right now, as if it had been pressed (not for jog wheels)."""
    ctrl = parse_control_id(control)
    m = _config().mappings.get(ctrl)
    if m is None:
        raise ToolError(f"{control_name(ctrl)} isn't mapped.")
    if m.scroll is not None or m.keys_cw is not None:
        raise ToolError("Jog wheel mappings can only be tried by turning the wheel.")
    msg = _fake_press(ctrl)
    Mapper(_config(), Executor()).handle(msg)
    return {"ran": _describe(m)}


def _fake_press(ctrl: Control):
    import mido

    if ctrl.kind == "note":
        return mido.Message("note_on", channel=ctrl.channel - 1, note=ctrl.number, velocity=127)
    return mido.Message("control_change", channel=ctrl.channel - 1, control=ctrl.number, value=127)


@tool(WRITE)
def start_mapper() -> dict[str, Any]:
    """Start the mapper in the background (if it isn't running) so mappings work. It keeps running after you exit."""
    return _ensure_mapper()


@tool(DESTRUCTIVE)
def stop_mapper() -> dict[str, Any]:
    """Stop the background mapper; the FLX4's buttons stop doing anything outside DJ software."""
    pid = service.stop_background(config_path)
    return {"stopped_pid": pid, "was_running": pid is not None}


def serve(path: Path = DEFAULT_CONFIG) -> None:
    global config_path
    config_path = Path(path)
    mcp.run()

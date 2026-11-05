"""Config file format, parsing and validation."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "midi-mapper-flx4"
DEFAULT_CONFIG = CONFIG_DIR / "config.toml"
DEFAULT_DEVICE = "DDJ-FLX4"  # overridable in the config file, mainly for tests with virtual ports
# Exact process names (case-insensitive). While any is running, mappings are paused so the
# controller only drives the DJ app. Note: "rekordboxAgent" (a background helper) is deliberately absent.
DEFAULT_PAUSE = ["rekordbox", "Serato DJ Pro", "Serato DJ Lite"]

MODIFIERS = {
    "cmd": "cmd",
    "command": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "shift": "shift",
}
SPECIAL_KEYS = {
    "enter": "enter",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "page_up": "page_up",
    "pagedown": "page_down",
    "page_down": "page_down",
    "plus": "+",
    **{f"f{i}": f"f{i}" for i in range(1, 21)},
}
ENCODINGS = ("offset64", "twos")


class ConfigError(ValueError):
    pass


def parse_combo(combo: str) -> tuple[tuple[str, ...], str]:
    """'cmd+shift+p' -> (('cmd', 'shift'), 'p'). Use 'plus' for the + key."""
    parts = [p.strip().lower() for p in combo.split("+")]
    if not parts or any(not p for p in parts):
        raise ConfigError(f"Bad key combo {combo!r} (write the + key as 'plus')")
    *mods, key = parts
    bad = [m for m in mods if m not in MODIFIERS]
    if bad:
        raise ConfigError(f"Unknown modifier(s) {bad} in {combo!r}; use cmd, ctrl, alt/option, shift")
    if key in SPECIAL_KEYS:
        key = SPECIAL_KEYS[key]
    elif len(key) != 1:
        raise ConfigError(f"Unknown key {key!r} in {combo!r}")
    return tuple(dict.fromkeys(MODIFIERS[m] for m in mods)), key


@dataclass(frozen=True)
class Control:
    kind: str  # "note" | "cc"
    channel: int  # 1-16, as printed on most MIDI docs (mido is 0-based)
    number: int

    @classmethod
    def from_message(cls, msg) -> Control | None:
        if msg.type == "note_on" and msg.velocity > 0:
            return cls("note", msg.channel + 1, msg.note)
        if msg.type == "control_change":
            return cls("cc", msg.channel + 1, msg.control)
        return None

    def __str__(self) -> str:
        return f"{self.kind} {self.number} on channel {self.channel}"


@dataclass
class Mapping:
    control: Control
    name: str = ""
    keys: str | None = None
    text: str | None = None
    shell: str | None = None
    app: str | None = None  # application to open or bring to the front
    scroll: int | None = None  # lines per encoder step (cc only)
    keys_cw: str | None = None  # combo per clockwise step (cc only)
    keys_ccw: str | None = None
    menu: str | None = None  # "Window > Next Session" (front app) or "Rex > Window > Next Session"
    menu_cw: str | None = None  # a menu item per clockwise step (cc only)...
    menu_ccw: str | None = None  # ...and per counter-clockwise step
    encoding: str = "offset64"
    ticks: int | None = None  # jog/encoder movement per step (cc only); None = the default for the action

    @property
    def label(self) -> str:
        return self.name or str(self.control)


@dataclass
class Config:
    device: str = DEFAULT_DEVICE
    pause_when_running: list[str] = field(default_factory=lambda: list(DEFAULT_PAUSE))
    mappings: dict[Control, Mapping] = field(default_factory=dict)


def parse_config(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML: {e}") from e
    cfg = Config(
        device=data.get("device", DEFAULT_DEVICE),
        pause_when_running=list(data.get("pause_when_running", DEFAULT_PAUSE)),
    )
    for i, m in enumerate(data.get("map", []), 1):
        where = f"[[map]] #{i}" + (f" ({m['name']})" if m.get("name") else "")
        kinds = [k for k in ("note", "cc") if k in m]
        if len(kinds) != 1:
            raise ConfigError(f"{where}: needs exactly one of note = N or cc = N")
        kind = kinds[0]
        channel = m.get("channel", 1)
        if not (isinstance(channel, int) and 1 <= channel <= 16):
            raise ConfigError(f"{where}: channel must be 1-16")
        number = m[kind]
        if not (isinstance(number, int) and 0 <= number <= 127):
            raise ConfigError(f"{where}: {kind} must be 0-127")
        ctrl = Control(kind, channel, number)

        actions = [a for a in ("keys", "text", "shell", "app", "menu", "scroll", "keys_cw", "menu_cw") if a in m]
        for half, other in (("keys_ccw", "keys_cw"), ("menu_ccw", "menu_cw")):
            if half in m and other not in m:
                actions.append(half)
        if len(actions) != 1:
            raise ConfigError(
                f"{where}: needs exactly one action: keys, text, shell, app, scroll, or keys_cw + keys_ccw"
            )
        action = actions[0]
        if action in ("scroll", "keys_cw", "keys_ccw", "menu_cw", "menu_ccw") and kind != "cc":
            raise ConfigError(f"{where}: {action} only works on a cc control (jog wheel / encoder)")
        if action in ("keys_cw", "keys_ccw") and not ("keys_cw" in m and "keys_ccw" in m):
            raise ConfigError(f"{where}: keys_cw and keys_ccw must be given together")
        if action in ("menu_cw", "menu_ccw") and not ("menu_cw" in m and "menu_ccw" in m):
            raise ConfigError(f"{where}: menu_cw and menu_ccw must be given together")
        for k in ("menu", "menu_cw", "menu_ccw"):
            if k in m and (not isinstance(m[k], str) or len([p for p in m[k].split(">") if p.strip()]) < 2):
                raise ConfigError(f'{where}: {k} must look like "Window > Next Session" or "App > Menu > Item"')
        for k in ("keys", "keys_cw", "keys_ccw"):
            if k in m:
                parse_combo(m[k])
        if "app" in m and not (isinstance(m["app"], str) and m["app"].strip()):
            raise ConfigError(f'{where}: app must be an application name, e.g. "Discord"')
        if "scroll" in m and not isinstance(m["scroll"], int):
            raise ConfigError(f"{where}: scroll must be a whole number of lines per step")
        ticks = m.get("ticks")
        if ticks is not None and not (isinstance(ticks, int) and 1 <= ticks <= 1000):
            raise ConfigError(f"{where}: ticks must be a whole number from 1 to 1000")
        if ticks is not None and action not in ("scroll", "keys_cw", "keys_ccw", "menu_cw", "menu_ccw"):
            raise ConfigError(f"{where}: ticks only applies to scroll and keys_cw/keys_ccw (jog wheels, knobs)")
        enc = m.get("encoding", "offset64")
        if enc not in ENCODINGS:
            raise ConfigError(f"{where}: encoding must be one of {ENCODINGS}")
        if ctrl in cfg.mappings:
            raise ConfigError(f"{where}: {ctrl} is already mapped as {cfg.mappings[ctrl].label!r}")

        cfg.mappings[ctrl] = Mapping(
            control=ctrl,
            name=m.get("name", ""),
            keys=m.get("keys"),
            text=m.get("text"),
            shell=m.get("shell"),
            app=m.get("app"),
            menu=m.get("menu"),
            menu_cw=m.get("menu_cw"),
            menu_ccw=m.get("menu_ccw"),
            scroll=m.get("scroll"),
            keys_cw=m.get("keys_cw"),
            keys_ccw=m.get("keys_ccw"),
            encoding=enc,
            ticks=ticks,
        )
    return cfg


def load_config(path: Path) -> Config:
    return parse_config(Path(path).read_text())


def starter_config(device: str = DEFAULT_DEVICE, pause_when_running: list[str] | None = None) -> str:
    pause = DEFAULT_PAUSE if pause_when_running is None else pause_when_running
    device_line = "" if device == DEFAULT_DEVICE else f"device = {json.dumps(device)}\n"
    return f"""# midi-mapper-flx4 config for a Pioneer DDJ-FLX4. Map buttons by asking your AI assistant (MCP),
# with `midi-mapper-flx4 map` (interactive diagram) or `midi-mapper-flx4 gui`, or edit by hand.
#
# Each [[map]] has one control (note = N or cc = N, plus channel = 1-16) and one action:
#   keys   = "cmd+shift+p"        key combo (modifiers: cmd, ctrl, alt/option, shift)
#   text   = "/clear\\n"           typed text; \\n presses Enter
#   shell  = "say done"           shell command, run in the background
#   app    = "Discord"            open the app, or bring it to the front
#   scroll = 3                    jog wheel / encoder: lines scrolled per step
#   keys_cw = "down"              jog wheel / encoder: combo per clockwise step...
#   keys_ccw = "up"               ...and per counter-clockwise step

{device_line}pause_when_running = {json.dumps(pause)}
"""


def mapping_block(ctrl: Control, action: dict[str, str | int], name: str = "") -> str:
    lines = ["", "[[map]]"]
    if name:
        lines.append(f"name = {json.dumps(name)}")
    lines += [f"{ctrl.kind} = {ctrl.number}", f"channel = {ctrl.channel}"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in action.items()]
    return "\n".join(lines) + "\n"


def mapping_action(m: Mapping) -> dict[str, str | int]:
    """The config keys that describe a mapping's action."""
    if m.keys_cw is not None:
        action: dict[str, str | int] = {"keys_cw": m.keys_cw, "keys_ccw": m.keys_ccw}
    elif m.menu_cw is not None:
        action = {"menu_cw": m.menu_cw, "menu_ccw": m.menu_ccw}
    elif m.menu is not None:
        action = {"menu": m.menu}
    elif m.scroll is not None:
        action = {"scroll": m.scroll}
    elif m.keys is not None:
        action = {"keys": m.keys}
    elif m.text is not None:
        action = {"text": m.text}
    elif m.app is not None:
        action = {"app": m.app}
    else:
        action = {"shell": m.shell}
    if m.ticks is not None:
        action["ticks"] = m.ticks
    if m.encoding != "offset64":
        action["encoding"] = m.encoding
    return action


def render_config(cfg: Config) -> str:
    """Serialise a config. Hand-written comments are not preserved."""
    text = starter_config(cfg.device, cfg.pause_when_running)
    for m in cfg.mappings.values():
        text += mapping_block(m.control, mapping_action(m), m.name)
    return text


def mapping_to_json(m: Mapping) -> dict:
    a = mapping_action(m)
    if "keys_cw" in a:
        action = {"type": "turn", "cw": a["keys_cw"], "ccw": a["keys_ccw"]}
    elif "menu_cw" in a:
        action = {"type": "turn_menu", "cw": a["menu_cw"], "ccw": a["menu_ccw"]}
    else:
        kind = next(k for k in ("keys", "text", "shell", "app", "menu", "scroll") if k in a)
        action = {"type": kind, "value": a[kind]}
    c = m.control
    return {
        "control": {"kind": c.kind, "channel": c.channel, "number": c.number},
        "name": m.name,
        "label": m.label,
        "action": action,
        "encoding": m.encoding,
        "ticks": m.ticks,
    }


def control_from_json(d: dict) -> Control:
    try:
        return Control(str(d["kind"]), int(d["channel"]), int(d["number"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"Bad control {d!r}") from e


def mapping_from_json(d: dict) -> Mapping:
    """Build a Mapping from GUI JSON, validating it through the same path as the config file."""
    ctrl = control_from_json(d.get("control") or {})
    a = d.get("action") or {}
    kind = a.get("type")
    if kind in ("keys", "text", "shell", "app", "menu"):
        action: dict[str, str | int] = {kind: str(a.get("value") or "")}
        if kind != "text" and not action[kind].strip():
            raise ConfigError(f"{kind} can't be empty")
    elif kind == "scroll":
        try:
            action = {"scroll": int(a.get("value"))}
        except (TypeError, ValueError) as e:
            raise ConfigError("scroll needs a whole number of lines") from e
    elif kind == "turn":
        action = {"keys_cw": str(a.get("cw") or ""), "keys_ccw": str(a.get("ccw") or "")}
    elif kind == "turn_menu":
        action = {"menu_cw": str(a.get("cw") or ""), "menu_ccw": str(a.get("ccw") or "")}
    else:
        raise ConfigError(f"Unknown action type {kind!r}")
    if d.get("ticks") is not None:
        try:
            action["ticks"] = int(d["ticks"])
        except (TypeError, ValueError) as e:
            raise ConfigError("ticks must be a whole number") from e
    if d.get("encoding", "offset64") != "offset64":
        action["encoding"] = d["encoding"]
    cfg = parse_config(mapping_block(ctrl, action, str(d.get("name") or "").strip()))
    return cfg.mappings[ctrl]

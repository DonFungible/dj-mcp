"""midi-mapper-flx4 command line. With no command it runs the MCP server over stdio."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from .config import (
    DEFAULT_CONFIG,
    DEFAULT_DEVICE,
    ConfigError,
    Control,
    load_config,
    mapping_block,
    parse_config,
    starter_config,
)
from .engine import accessibility_trusted, find_port, log

ACTION_HELP = """\
  keys:<combo>       e.g. keys:cmd+shift+p   keys:ctrl+c   keys:enter
  text:<text>        e.g. text:/clear\\n      (\\n presses Enter)
  shell:<command>    e.g. shell:cd ~/code/app && uv run pytest
  app:<name>         e.g. app:Discord   (opens it, or brings it to the front)
  scroll:<lines>     jog wheel / knob: lines per step, e.g. scroll:3
  turn:<cw>,<ccw>    jog wheel / knob: combo per step, e.g. turn:down,up
  (blank to skip)"""


def parse_action(answer: str) -> dict[str, str | int]:
    kind, sep, value = answer.partition(":")
    kind = kind.strip().lower()
    if not sep:
        raise ConfigError("Write it as kind:value, e.g. keys:cmd+c")
    if kind in ("keys", "k"):
        return {"keys": value.strip()}
    if kind in ("text", "t"):
        return {"text": value.replace("\\n", "\n")}
    if kind in ("shell", "s"):
        return {"shell": value.strip()}
    if kind in ("app", "a"):
        return {"app": value.strip()}
    if kind == "scroll":
        try:
            return {"scroll": int(value)}
        except ValueError as e:
            raise ConfigError("scroll needs a whole number, e.g. scroll:3") from e
    if kind == "turn":
        cw, _, ccw = value.partition(",")
        if not ccw:
            raise ConfigError("turn needs two combos, e.g. turn:down,up")
        return {"keys_cw": cw.strip(), "keys_ccw": ccw.strip()}
    raise ConfigError(f"Unknown action {kind!r}")


def cmd_devices(_args) -> None:

    print("MIDI inputs:", *(_input_names() or ["(none)"]), sep="\n  ")


def cmd_learn(args) -> None:
    import mido

    path: Path = args.config
    ensure_config(path)
    cfg = load_config(path)
    port_name = find_port(cfg.device)
    print(f"Listening on {port_name}. Press a control on the controller (Ctrl+C to finish).")
    print("Tip: Shift + a button sends a different message, so it can have its own mapping.\n")

    recent: dict[Control, float] = {}
    with mido.open_input(port_name) as port:
        while True:
            msg = port.receive()
            ctrl = Control.from_message(msg)
            if ctrl is None:
                continue
            now = time.monotonic()
            # Jogs/knobs/faders send streams; treat a burst from one control as one event.
            if now - recent.get(ctrl, 0) < 1.0:
                recent[ctrl] = now
                continue
            recent[ctrl] = now

            raw = f"value={msg.value}" if ctrl.kind == "cc" else ""
            print(f"\n▶ {ctrl} {raw}".rstrip())
            cfg = load_config(path)
            if ctrl in cfg.mappings:
                print(f"  already mapped as {cfg.mappings[ctrl].label!r}; edit {path} to change it.")
                continue
            if ctrl.kind == "cc":
                print("  (looks like a jog wheel, knob or fader; scroll/turn suit jogs and knobs)")
            print(ACTION_HELP)
            answer = input("  action> ").strip()
            if answer:
                try:
                    action = parse_action(answer)
                    name = input("  name (optional)> ").strip()
                    block = mapping_block(ctrl, action, name)
                    parse_config(path.read_text() + block)  # validate before saving
                except ConfigError as e:
                    print(f"  ✗ {e}. Not saved; press the control again to retry.")
                else:
                    with open(path, "a") as f:
                        f.write(block)
                    print(f"  ✓ saved to {path}")
            for _ in port.iter_pending():  # drop anything pressed while typing
                pass
            recent.clear()


def cmd_check(args) -> None:
    cfg = load_config(args.config)
    print(f"{args.config}: OK. device={cfg.device!r}, {len(cfg.mappings)} mapping(s)")
    for m in cfg.mappings.values():
        action = next(
            f"{k}={v!r}"
            for k, v in (
                ("keys", m.keys),
                ("text", m.text),
                ("shell", m.shell),
                ("app", m.app),
                ("scroll", m.scroll),
                ("keys_cw/ccw", m.keys_cw and f"{m.keys_cw}/{m.keys_ccw}"),
            )
            if v is not None
        )
        print(f"  {m.label:30} {action}")


def ensure_config(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(starter_config())
        print(f"Created {path}", file=sys.stderr)


def _warn_accessibility(dry_run: bool) -> None:
    if not dry_run and accessibility_trusted() is False:
        log("WARNING: this terminal/Python lacks Accessibility permission, so keystrokes and scrolling will be")
        log("ignored by macOS. Grant it in System Settings → Privacy & Security → Accessibility, then restart.")


def cmd_run(args) -> None:
    from .runtime import Runtime

    ensure_config(args.config)
    _warn_accessibility(args.dry_run)
    runtime = Runtime(args.config, dry_run=args.dry_run)
    if not any(runtime.config.device.lower() in n.lower() for n in _input_names()):
        log(f"waiting for the {runtime.config.device} to be connected...")
    runtime.start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so `stop` runs the cleanup below
    try:
        while True:
            time.sleep(3600)
    finally:
        runtime.stop()


def cmd_gui(args) -> None:
    from .gui import serve
    from .runtime import Runtime

    ensure_config(args.config)
    _warn_accessibility(args.dry_run)
    serve(Runtime(args.config, dry_run=args.dry_run), port=args.port, open_browser=not args.no_browser)


def cmd_stop(args) -> None:
    from .service import stop_background

    pid = stop_background(args.config)
    print(f"Stopped the mapper (pid {pid})." if pid else "The mapper isn't running.")


def cmd_status(args) -> None:
    from .service import running_pid

    pid = running_pid(args.config)
    connected = any(DEFAULT_DEVICE.lower() in n.lower() for n in _input_names())
    print(f"mapper: {'running (pid ' + str(pid) + ')' if pid else 'not running'}")
    print(f"{DEFAULT_DEVICE}: {'connected' if connected else 'not connected'}")
    print(f"config: {args.config}")


def cmd_serve(args) -> None:
    from .server import serve

    ensure_config(args.config)
    serve(args.config)


def _input_names() -> list[str]:
    from .engine import input_names

    return input_names()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="midi-mapper-flx4",
        description="Map Pioneer DDJ-FLX4 buttons to keys, text, apps and commands. "
        "With no command, runs the MCP server for AI assistants.",
    )
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    ap.set_defaults(func=cmd_serve)
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(func=cmd_serve)
    sub.add_parser("devices", help="list connected MIDI inputs").set_defaults(func=cmd_devices)
    sub.add_parser("learn", help="press buttons and assign actions in the terminal").set_defaults(func=cmd_learn)
    p = sub.add_parser("run", help="run the mapper in this terminal")
    p.add_argument("--dry-run", action="store_true", help="log actions instead of performing them")
    p.set_defaults(func=cmd_run)
    sub.add_parser("check", help="validate the config and list mappings").set_defaults(func=cmd_check)
    p = sub.add_parser("gui", help="open the mapping editor in your browser (also runs the mappings)")
    p.add_argument("--port", type=int, default=0, help="port to serve on (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    p.add_argument("--dry-run", action="store_true", help="log actions instead of performing them")
    p.set_defaults(func=cmd_gui)
    sub.add_parser("stop", help="stop a mapper running in the background").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="is the mapper running and the FLX4 connected?").set_defaults(func=cmd_status)
    args = ap.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print()
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}") from e
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    except FileNotFoundError as e:
        raise SystemExit(f"{e.filename} not found. Run `midi-mapper-flx4 learn` to create it.") from e


if __name__ == "__main__":
    main()

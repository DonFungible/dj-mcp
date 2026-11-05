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
    load_config,
    starter_config,
)
from .engine import accessibility_trusted, log


def cmd_devices(_args) -> None:

    print("MIDI inputs:", *(_input_names() or ["(none)"]), sep="\n  ")


def cmd_map(args) -> None:
    from .tui import run

    ensure_config(args.config)
    run(args.config)


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


def cmd_permissions(_args) -> None:
    from . import permissions

    if permissions.can_send_keys() is None:
        print("Nothing to set up: permissions are only needed on macOS.")
        return
    keys = permissions.request_send_keys()
    listen = permissions.request_listen()
    print(f"Accessibility (send keystrokes, scroll): {'allowed' if keys else 'not yet - macOS is asking'}")
    print(f"Input Monitoring (record shortcuts):     {'allowed' if listen else 'not yet - macOS is asking'}")
    if not (keys and listen):
        print("Turn on this terminal app in the prompts (or System Settings > Privacy & Security), then restart the")
        print("mapper: midi-mapper-flx4 stop, then start it again from this terminal.")


def cmd_start(args) -> None:
    from .service import running_pid, start_background

    ensure_config(args.config)
    already = running_pid(args.config)
    pid = start_background(args.config)
    print(f"The mapper is already running (pid {pid})." if already else f"Started the mapper (pid {pid}).")


def cmd_stop(args) -> None:
    from .service import stop_background

    pid = stop_background(args.config)
    print(f"Stopped the mapper (pid {pid})." if pid else "The mapper isn't running.")


def cmd_status(args) -> None:
    from .service import running_pid

    pid = running_pid(args.config)
    connected = any(DEFAULT_DEVICE.lower() in n.lower() for n in _input_names())
    print(f"mapper: {'running (pid ' + str(pid) + ')' if pid else 'not running'}")
    if pid:
        from .service import KEYSTROKE_HELP, mapper_can_type

        can_type = mapper_can_type(args.config)
        if can_type is False:
            print(f"keystrokes: blocked. {KEYSTROKE_HELP}")
        elif can_type:
            print("keystrokes: allowed")
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
    sub.add_parser("map", help="interactive FLX4 diagram: press a control, choose what it does").set_defaults(
        func=cmd_map
    )
    sub.add_parser("learn", help="same as map").set_defaults(func=cmd_map)
    p = sub.add_parser("run", help="run the mapper in this terminal")
    p.add_argument("--dry-run", action="store_true", help="log actions instead of performing them")
    p.set_defaults(func=cmd_run)
    sub.add_parser("check", help="validate the config and list mappings").set_defaults(func=cmd_check)
    p = sub.add_parser("gui", help="open the mapping editor in your browser (also runs the mappings)")
    p.add_argument("--port", type=int, default=0, help="port to serve on (default: any free port)")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    p.add_argument("--dry-run", action="store_true", help="log actions instead of performing them")
    p.set_defaults(func=cmd_gui)
    sub.add_parser("start", help="start the mapper in the background").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop a mapper running in the background").set_defaults(func=cmd_stop)
    sub.add_parser(
        "permissions", help="ask macOS for the permissions keystrokes and shortcut recording need"
    ).set_defaults(func=cmd_permissions)
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
        raise SystemExit(f"{e.filename} not found. Run `midi-mapper-flx4 map` to create it.") from e


if __name__ == "__main__":
    main()

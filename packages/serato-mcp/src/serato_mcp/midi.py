"""Deck/mixer control over MIDI.

serato-mcp opens a virtual MIDI device ("Serato MCP") that Serato DJ Pro sees like any controller. Every
action has a fixed MIDI message (see ACTIONS); the user maps each one once in Serato's MIDI assign mode,
using `midi_learn` to send the message on demand. After that the tools drive Serato directly.

Layout: deck N uses MIDI channel N (1-4). Mixer/global/library controls use channel 16. Buttons are notes
(note-on 127, then note-off); faders/knobs are CCs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from . import coremidi
from .jsnum import js_round

PORT_NAME = os.environ.get("SERATO_MCP_MIDI_PORT") or "Serato MCP"
GLOBAL_CH = 16


@dataclass
class ActionDef:
    kind: str  # button | fader
    scope: str  # deck | global
    number: int  # note or CC
    description: str
    serato: str  # where to click in Serato's MIDI assign mode


def _btn(number: int, description: str, serato: str, scope: str = "deck") -> ActionDef:
    return ActionDef("button", scope, number, description, serato)


def _fader(number: int, description: str, serato: str, scope: str = "deck") -> ActionDef:
    return ActionDef("fader", scope, number, description, serato)


ACTIONS: dict[str, ActionDef] = {
    # transport
    "play_pause": _btn(0, "Play / pause", "Deck play button"),
    "cue": _btn(1, "Cue (temporary cue)", "Deck cue button"),
    "sync": _btn(2, "Sync on/off", "Deck SYNC button"),
    "load_selected": _btn(
        3,
        "Load the highlighted library track onto this deck",
        "Load-to-deck: assign via Library > 'Load to Deck N' in MIDI assign",
    ),
    "key_lock": _btn(4, "Key lock on/off", "Deck key lock (padlock) button"),
    "censor": _btn(5, "Censor / reverse while held", "Deck censor button"),
    "slip": _btn(6, "Slip mode on/off", "Deck slip button"),
    "eject": _btn(7, "Eject track", "Deck eject button"),
    # hot cues 1-8
    **{
        f"hotcue_{i + 1}": _btn(
            10 + i, f"Trigger hot cue {i + 1} (sets it if empty)", f"Hot cue {i + 1} pad on the deck"
        )
        for i in range(8)
    },
    **{
        f"hotcue_{i + 1}_delete": _btn(18 + i, f"Delete hot cue {i + 1}", f"Hot cue {i + 1} delete (x) button")
        for i in range(8)
    },
    # loops
    "loop_in": _btn(30, "Manual loop in", "Loop IN"),
    "loop_out": _btn(31, "Manual loop out", "Loop OUT"),
    "reloop": _btn(32, "Reloop / exit loop", "Reloop / loop on-off"),
    "loop_halve": _btn(33, "Halve loop length", "Auto loop ÷2 (decrease)"),
    "loop_double": _btn(34, "Double loop length", "Auto loop ×2 (increase)"),
    "autoloop_toggle": _btn(35, "Auto loop on/off at the selected size", "Auto loop on/off button"),
    **{
        f"autoloop_{b}": _btn(36 + i, f"Set a {b}-beat auto loop", f"Auto loop size {b}")
        for i, b in enumerate((1, 2, 4, 8, 16, 32))
    },
    "beatjump_forward": _btn(42, "Beat jump forward", "Beat jump >"),
    "beatjump_back": _btn(43, "Beat jump back", "Beat jump <"),
    # tempo / key
    "pitch_reset": _btn(50, "Reset tempo to 0%", "Tempo reset (double-click the pitch %)"),
    "nudge_up": _btn(51, "Temporary pitch bend up (held for ~250ms)", "Pitch bend +"),
    "nudge_down": _btn(52, "Temporary pitch bend down (held for ~250ms)", "Pitch bend -"),
    "key_up": _btn(53, "Key shift up a semitone", "Key shift +"),
    "key_down": _btn(54, "Key shift down a semitone", "Key shift -"),
    "key_reset": _btn(55, "Reset key shift", "Key reset"),
    "key_sync": _btn(56, "Key sync to the other deck", "Key sync"),
    # effects (per deck assign)
    "fx1_assign": _btn(60, "Assign FX unit 1 to this deck", "FX 1 deck assign button"),
    "fx2_assign": _btn(61, "Assign FX unit 2 to this deck", "FX 2 deck assign button"),
    # faders (CC, value 0-127)
    "tempo": _fader(1, "Tempo fader (64 = 0%)", "Deck tempo slider"),
    "volume": _fader(7, "Channel volume", "Channel fader"),
    "gain": _fader(8, "Channel gain/trim", "Channel gain knob"),
    "eq_high": _fader(16, "EQ high (64 = flat)", "EQ high knob"),
    "eq_mid": _fader(17, "EQ mid (64 = flat)", "EQ mid knob"),
    "eq_low": _fader(18, "EQ low (64 = flat)", "EQ low knob"),
    "filter": _fader(19, "Filter (64 = off, lower = low-pass, higher = high-pass)", "Channel filter knob"),
    # global / mixer (channel 16)
    "crossfader": _fader(8, "Crossfader (0 = left, 64 = centre, 127 = right)", "Crossfader", "global"),
    "master_volume": _fader(7, "Master volume", "Master volume", "global"),
    "fx1_depth": _fader(20, "FX unit 1 depth / wet-dry", "FX 1 depth knob", "global"),
    "fx2_depth": _fader(21, "FX unit 2 depth / wet-dry", "FX 2 depth knob", "global"),
    "fx1_on": _btn(20, "FX unit 1 effect 1 on/off", "FX 1 slot 1 on button", "global"),
    "fx2_on": _btn(21, "FX unit 2 effect 1 on/off", "FX 2 slot 1 on button", "global"),
    "library_up": _btn(0, "Move the library selection up", "Library browse up", "global"),
    "library_down": _btn(1, "Move the library selection down", "Library browse down", "global"),
    "crate_up": _btn(2, "Move the crate selection up", "Crate browse up", "global"),
    "crate_down": _btn(3, "Move the crate selection down", "Crate browse down", "global"),
    "library_toggle_focus": _btn(4, "Switch focus between crates and tracks", "Library focus toggle", "global"),
    "record": _btn(10, "Start/stop recording", "Record button", "global"),
    "sampler_1": _btn(30, "Fire sampler slot 1", "Sample player slot 1", "global"),
    "sampler_2": _btn(31, "Fire sampler slot 2", "Sample player slot 2", "global"),
    "sampler_3": _btn(32, "Fire sampler slot 3", "Sample player slot 3", "global"),
    "sampler_4": _btn(33, "Fire sampler slot 4", "Sample player slot 4", "global"),
}


class MidiBridge:
    def __init__(self) -> None:
        self._client: coremidi.Client | None = None
        self._source: int | None = None  # our virtual device, or None when sending to an existing port
        self._port: int | None = None
        self._dest_index: int | None = None
        self._input: int | None = None
        self.virtual = False
        self._log: list[dict] = []
        self.feedback: dict[str, int] = {}
        self._lock = threading.Lock()

    def open(self) -> dict:
        with self._lock:
            if self._client is not None:
                return {"port": PORT_NAME, "virtual": self.virtual}
            client = coremidi.Client("serato-mcp")
            # Use an existing port of that name (e.g. "IAC Driver Bus 1") if there is one.
            dests = coremidi.destination_names()
            idx = next((i for i, n in enumerate(dests) if n == PORT_NAME or n.endswith(PORT_NAME)), None)
            if idx is not None:
                self._port = client.output_port()
                self._dest_index = idx
            else:
                self._source = client.virtual_source(PORT_NAME)
                self.virtual = True
            # Listen for Serato's MIDI output (LED feedback) on a matching virtual destination.
            with contextlib.suppress(coremidi.MIDIError):  # feedback is optional
                self._input = client.virtual_destination(PORT_NAME, self._on_feedback)
            self._client = client
            return {"port": PORT_NAME, "virtual": self.virtual}

    def _on_feedback(self, msg: list[int]) -> None:
        now = datetime.now(UTC)
        self._log.append({"at": now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z", "bytes": msg})
        if len(self._log) > 200:
            self._log.pop(0)
        if len(msg) >= 3:
            self.feedback[f"{msg[0]}:{msg[1]}"] = msg[2]

    def ports(self) -> dict:
        try:
            return {"outputs": coremidi.destination_names(), "inputs": coremidi.source_names()}
        except coremidi.MIDIError:
            return {"outputs": [], "inputs": []}

    def send(self, data: list[int]) -> None:
        self.open()
        assert self._client is not None
        if self._source is not None:
            self._client.received(self._source, bytes(data))
        else:
            self._client.send(self._port, self._dest_index, bytes(data))  # type: ignore[arg-type]

    def received(self, limit: int = 50) -> list[dict]:
        return self._log[-limit:]

    def close(self) -> None:
        with self._lock:
            if self._client is None:
                return
            for ep in (self._source, self._input):
                if ep is not None:
                    self._client.dispose_endpoint(ep)
            if self._port is not None:
                self._client.dispose_port(self._port)
            self._client = self._source = self._input = self._port = None
            self.virtual = False


bridge = MidiBridge()


def _channel_for(d: ActionDef, deck: int | None) -> int:
    if d.scope == "global":
        return GLOBAL_CH
    if not deck or deck < 1 or deck > 4:
        raise ValueError("deck must be 1-4 for this action")
    return deck


def message_for(action: str, deck: int | None = None, value: float | None = None) -> dict:
    d = ACTIONS.get(action)
    if not d:
        raise ValueError(f'Unknown action "{action}". Known: {", ".join(ACTIONS)}')
    ch = _channel_for(d, deck) - 1
    if d.kind == "button":
        return {"action": action, "channel": ch + 1, "bytes": [0x90 | ch, d.number, 127]}
    v = max(0, min(127, js_round(64 if value is None else value)))
    return {"action": action, "channel": ch + 1, "bytes": [0xB0 | ch, d.number, v]}


async def perform(
    action: str, deck: int | None = None, value: float | None = None, hold_ms: float | None = None
) -> dict:
    """Press a button (note on, hold, note off) or move a fader."""
    msg = message_for(action, deck, value)
    bridge.send(msg["bytes"])
    if ACTIONS[action].kind == "button":
        hold = hold_ms if hold_ms is not None else (250 if action.startswith("nudge") else 40)
        await asyncio.sleep(hold / 1000)
        bridge.send([0x80 | (msg["channel"] - 1), msg["bytes"][1], 0])
    return msg


async def sweep(action: str, deck: int | None, start: float, end: float, duration_ms: float) -> int:
    """Smoothly move a fader from one value to another over `duration_ms`."""
    steps = max(1, min(200, js_round(duration_ms / 25)))
    for i in range(steps + 1):
        bridge.send(message_for(action, deck, start + (end - start) * i / steps)["bytes"])
        if i < steps:
            await asyncio.sleep(duration_ms / steps / 1000)
    return steps + 1


def tempo_value(pct: float, range_pct: float = 8) -> int:
    """Map a pitch percentage to the tempo fader value for a given pitch range (±8% by default)."""
    clamped = max(-range_pct, min(range_pct, pct))
    # 0 = -range, 64 = 0%, 127 = +range (the + side has one step fewer).
    v = 64 + clamped / range_pct * 63 if clamped >= 0 else 64 + clamped / range_pct * 64
    return max(0, min(127, js_round(v)))


def mapping_table() -> list[dict]:
    return [
        {
            "action": name,
            "type": "note" if d.kind == "button" else "CC",
            "channel": str(GLOBAL_CH) if d.scope == "global" else "deck number (1-4)",
            "number": d.number,
            "description": d.description,
            "mapTo": d.serato,
        }
        for name, d in ACTIONS.items()
    ]

"""Pioneer DDJ-FLX4 control names, so mappings read "Deck 1 Play/Pause" instead of "note 11 on channel 1".

Channels are 1-based. Decks 1 and 2 use channels 1 and 2; their performance pads use 8 and 10
(9 and 11 while Shift is held). Controls not listed here still work; they just get a generic name.
"""

from __future__ import annotations

from .config import Control

DEVICE = "DDJ-FLX4"

DECK_NOTES = {
    11: "Play/Pause",
    12: "Cue",
    16: "Loop In",
    17: "Loop Out",
    27: "Hot Cue mode",
    30: "Pad FX 1 mode",
    32: "Beat Jump mode",
    34: "Sampler mode",
    54: "Jog wheel touch",
    71: "Shift + Play/Pause",
    72: "Shift + Cue",
    84: "Headphone Cue",
    88: "Beat Sync",
}
DECK_CCS = {33: "Jog wheel (side)", 34: "Jog wheel (top)"}
PAD_MODES = ["Hot Cue", "Pad FX 1", "Beat Jump", "Sampler", "Keyboard", "Pad FX 2", "Beat Loop", "Key Shift"]
PAD_CHANNELS = {8: (1, False), 9: (1, True), 10: (2, False), 11: (2, True)}  # channel -> (deck, shift)
JOG_CCS = {33, 34}


def control_name(c: Control) -> str:
    if c.channel in (1, 2):
        deck = f"Deck {c.channel}"
        known = (DECK_NOTES if c.kind == "note" else DECK_CCS).get(c.number)
        if known:
            return f"{deck} {known}"
        return f"{deck} {'button' if c.kind == 'note' else 'knob/fader'} ({c.kind} {c.number})"
    if c.channel in PAD_CHANNELS and c.kind == "note":
        deck, shift = PAD_CHANNELS[c.channel]
        mode, pad = divmod(c.number, 16)
        if mode < len(PAD_MODES) and pad < 8:
            return f"Deck {deck} {'Shift + ' if shift else ''}{PAD_MODES[mode]} pad {pad + 1}"
    if c.channel == 5 and c.kind == "note" and c.number == 71:
        return "Beat FX On/Off"
    return f"{c.kind} {c.number} on channel {c.channel}"


def control_id(c: Control) -> str:
    """Compact, stable id for tools: 'note:1:11'."""
    return f"{c.kind}:{c.channel}:{c.number}"


def parse_control_id(s: str) -> Control:
    from .config import ConfigError

    try:
        kind, channel, number = s.strip().split(":")
        c = Control(kind, int(channel), int(number))
    except ValueError as e:
        raise ConfigError(f"Bad control id {s!r}; expected e.g. 'note:1:11'") from e
    if kind not in ("note", "cc") or not (1 <= c.channel <= 16 and 0 <= c.number <= 127):
        raise ConfigError(f"Bad control id {s!r}")
    return c


def is_jog(c: Control) -> bool:
    return c.kind == "cc" and c.channel in (1, 2) and c.number in JOG_CCS

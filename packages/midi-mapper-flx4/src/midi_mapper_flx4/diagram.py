"""An ASCII drawing of the DDJ-FLX4 that marks mapped controls and the one being pressed.

The drawing follows the hardware: two decks (jog wheel, loop buttons, sync and tempo on the outer edge, mode
buttons, eight pads, play and cue) around a mixer (browse and load, channel knobs, headphone cues, channel
faders, crossfader). Every control is drawn as a button and changes shape with its state:

    [Play]   not mapped        (Play)   mapped        »Play«   pressed / selected

With color, mapped controls are green and the pressed one is highlighted; the frame and decoration are dim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Control
from .flx4 import PAD_CHANNELS, coarse

DECK_W, MIXER_W = 34, 24
SECTIONS = {"deck1": 0, "mixer": DECK_W + 1, "deck2": DECK_W + 1 + MIXER_W + 1}  # x offset inside the frame
WIDTH = DECK_W + 1 + MIXER_W + 1 + DECK_W  # inner width
ROWS = 15

RESET, DIM, GREEN, HILITE, BOLD = "\x1b[0m", "\x1b[2m", "\x1b[32m", "\x1b[1;30;43m", "\x1b[1m"
LEGEND = "(Play) mapped    »Play« selected    press a control to select it"


@dataclass
class Widget:
    key: str
    row: int
    col: int  # left edge of the cell the label is centered in
    cell: int  # cell width
    label: str
    ids: set[str] = field(default_factory=set)

    @property
    def start(self) -> int:
        return self.col + (self.cell - len(self.label) - 2) // 2


def _id(kind: str, channel: int, number: int) -> str:
    return f"{kind}:{channel}:{number}"


# ---- layout -----------------------------------------------------------------------------------------

# Terminal cells are about twice as tall as they are wide, so a round wheel is ~2.4x wider than tall.
JOG = [
    "    ╭───────╮    ",
    "  ╭─╯       ╰─╮  ",
    " ╭╯           ╰╮ ",
    " │             │ ",
    " ╰╮           ╭╯ ",
    "  ╰─╮       ╭─╯  ",
    "    ╰───────╯    ",
]
JOG_TOP = 2


def _deck(d: int) -> tuple[list[Widget], list[tuple[int, int, str]]]:
    x = SECTIONS[f"deck{d}"]
    n = lambda num: _id("note", d, num)  # noqa: E731
    pad_channels = [ch for ch, (deck, _) in PAD_CHANNELS.items() if deck == d]
    # Sync and tempo sit on the outer edge (left on deck 1, right on deck 2); the jog wheel fills the rest.
    edge = x + 1 if d == 1 else x + DECK_W - 8
    main = x + 9 if d == 1 else x + 1
    jog_x = main + (24 - len(JOG[0])) // 2
    widgets = [
        Widget(f"lin{d}", 1, main + 1, 11, "Loop in", {n(16)}),
        Widget(f"lout{d}", 1, main + 12, 11, "Loop out", {n(17)}),
        Widget(f"jog{d}", JOG_TOP + 3, jog_x, len(JOG[0]), "Jog", {_id("cc", d, 33), _id("cc", d, 34), n(54)}),
        Widget(f"sync{d}", 2, edge, 7, "Sync", {n(88)}),
        Widget(f"tempo{d}", 8, edge, 7, "Tempo", {_id("cc", d, 0)}),
        Widget(f"cue{d}", 14, x + 1, 8, "Cue", {n(12), n(72)}),
        Widget(f"play{d}", 14, x + 9, 8, "Play", {n(11), n(71)}),
    ]
    for i, (label, num) in enumerate((("Hotcue", 27), ("Pad FX", 30), ("Jump", 32), ("Sample", 34))):
        widgets.append(Widget(("hc", "pfx", "bj", "smp")[i] + str(d), 10, x + 1 + i * 8, 8, label, {n(num)}))
    for k in range(8):
        row, colno = (11, k) if k < 4 else (12, k - 4)
        ids = {_id("note", ch, mode * 16 + k) for ch in pad_channels for mode in range(8)}
        widgets.append(Widget(f"pad{d}_{k + 1}", row, x + 1 + colno * 8, 8, f"  {k + 1}  ", ids))
    art = [(JOG_TOP + i, jog_x, line) for i, line in enumerate(JOG)]
    rail = edge + 3
    art += [(3, rail, "┬"), (4, rail, "│"), (5, rail, "╪"), (6, rail, "│"), (7, rail, "┴")]
    return widgets, art


def _mixer() -> tuple[list[Widget], list[tuple[int, int, str]]]:
    x = SECTIONS["mixer"]
    left, mid, right = x, x + 8, x + 16
    widgets = [
        Widget("beatfx", 0, x, MIXER_W, "Beat FX", {_id("note", 5, 71)}),
        Widget("load1", 1, left, 8, "◀ Load", {_id("note", 7, 70)}),
        Widget("browse", 1, mid, 8, "Browse", {_id("cc", 7, 64)}),
        Widget("load2", 1, right, 8, "Load ▶", {_id("note", 7, 71)}),
        Widget("master", 3, mid, 8, "Master", {_id("cc", 7, 8)}),
        Widget("phones", 4, mid, 8, "Phones", {_id("cc", 7, 13)}),
        Widget("hpmix", 5, mid, 8, "HP mix", {_id("cc", 7, 12)}),
        Widget("xfader", 14, x, MIXER_W, "Crossfader", {_id("cc", 7, 31)}),
    ]
    for d, col in ((1, left), (2, right)):
        for row, label, cc in ((3, "Trim", 4), (4, "Hi", 7), (5, "Mid", 11), (6, "Low", 15)):
            widgets.append(Widget(f"{label.lower()}{d}", row, col, 8, label, {_id("cc", d, cc)}))
        widgets.append(Widget(f"cfx{d}", 7, col, 8, "CFX", {_id("cc", 7, 22 + d)}))
        widgets.append(Widget(f"hp{d}", 9, col, 8, "HP cue", {_id("note", d, 84)}))
        widgets.append(Widget(f"fader{d}", 13, col, 8, "Fader", {_id("cc", d, 19)}))
    art = []
    for col in (left + 3, right + 3):
        art += [(10, col, "┬"), (11, col, "╪"), (12, col, "┴")]
    art.append((13, mid, " ├──╪──┤"))
    return widgets, art


_d1, _a1 = _deck(1)
_d2, _a2 = _deck(2)
_mx, _am = _mixer()
WIDGETS: list[Widget] = _d1 + _d2 + _mx
ART: list[tuple[int, int, str]] = _a1 + _a2 + _am
BY_ID: dict[str, Widget] = {i: w for w in WIDGETS for i in w.ids}


def widget_for(c: Control) -> Widget | None:
    c = coarse(c)
    return BY_ID.get(f"{c.kind}:{c.channel}:{c.number}")


# ---- drawing ----------------------------------------------------------------------------------------


def _frame_top() -> str:
    parts = []
    for name, width in (("Deck 1", DECK_W), ("Mixer", MIXER_W), ("Deck 2", DECK_W)):
        parts.append(f"─ {name} ".ljust(width, "─"))
    return "╭" + "┬".join(parts) + "╮"


def _frame_bottom() -> str:
    return "╰" + "┴".join("─" * w for w in (DECK_W, MIXER_W, DECK_W)) + "╯"


def render(
    mapped: set[str] | None = None,
    pressed: Control | None = None,
    color: bool = False,
    legend: bool = True,
) -> str:
    """The controller drawing. `mapped` holds control ids ("note:1:11") that have a mapping."""
    mapped = mapped or set()
    grid = [[" "] * WIDTH for _ in range(ROWS)]
    style = [[""] * WIDTH for _ in range(ROWS)]

    def put(row: int, col: int, text: str, s: str = "") -> None:
        for i, ch in enumerate(text):
            if 0 <= col + i < WIDTH:
                grid[row][col + i] = ch
                style[row][col + i] = s

    for row, col, text in ART:
        put(row, col, text, DIM)
    hot = widget_for(pressed) if pressed else None
    for w in WIDGETS:  # same width in every state, so the layout never shifts
        if w is hot:
            put(w.row, w.start, f"»{w.label}«", HILITE)
        elif w.ids & mapped:
            put(w.row, w.start, f"({w.label})", GREEN)
        else:
            put(w.row, w.start, f"[{w.label}]")

    divider = f"{DIM}│{RESET}" if color else "│"
    lines = []
    for r in range(ROWS):
        line, current = "", ""
        for c in range(WIDTH):
            if c in (SECTIONS["mixer"] - 1, SECTIONS["deck2"] - 1):
                if color and current:
                    line += RESET
                    current = ""
                line += divider
                continue
            s = style[r][c] if color else ""
            if s != current:
                line += RESET if current else ""
                line += s
                current = s
            line += grid[r][c]
        if current:
            line += RESET
        lines.append(f"{divider}{line}{divider}")
    top, bottom = _frame_top(), _frame_bottom()
    if color:
        top, bottom = DIM + top + RESET, DIM + bottom + RESET
    out = [top, *lines, bottom]
    if legend:
        out.append(f"  {DIM}{LEGEND}{RESET}" if color else f"  {LEGEND}")
    return "\n".join(out)

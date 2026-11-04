"""An ASCII drawing of the DDJ-FLX4 that marks mapped controls and the one being pressed.

Each control on the drawing is a "widget": a bracketed label at a fixed spot, tied to the MIDI controls it
stands for. Widgets keep their width in every state, so the layout never shifts:

    [PLAY]   not mapped        (PLAY)   mapped        »PLAY«   pressed / selected

With color, mapped widgets are green and the pressed one is highlighted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Control
from .flx4 import PAD_CHANNELS, coarse

DECK_W = 32
MIXER_W = 22
WIDTH = 1 + DECK_W + 2 + MIXER_W + 2 + DECK_W + 1
ROWS = 13
MIXER_X = 1 + DECK_W + 2
DECK_X = {1: 1, 2: MIXER_X + MIXER_W + 2}

RESET, DIM, GREEN, HILITE = "\x1b[0m", "\x1b[2m", "\x1b[32m", "\x1b[1;30;43m"


@dataclass
class Widget:
    key: str
    row: int
    col: int
    width: int
    label: str
    ids: set[str] = field(default_factory=set)


def _id(kind: str, channel: int, number: int) -> str:
    return f"{kind}:{channel}:{number}"


def _deck(d: int) -> list[Widget]:
    x = DECK_X[d]
    n = lambda num: _id("note", d, num)  # noqa: E731
    pad_channels = [ch for ch, (deck, _) in PAD_CHANNELS.items() if deck == d]
    tempo_x = x if d == 1 else x + DECK_W - 7
    widgets = [
        Widget(f"lin{d}", 1, x + 1, 9, "LOOP IN", {n(16)}),
        Widget(f"lout{d}", 1, x + 11, 10, "LOOP OUT", {n(17)}),
        Widget(f"sync{d}", 1, x + 23, 8, "SYNC", {n(88)}),
        Widget(f"jog{d}", 5, x + 12, 9, "JOG", {_id("cc", d, 33), _id("cc", d, 34), n(54)}),
        Widget(f"tempo{d}", 8, tempo_x, 7, "TEMPO", {_id("cc", d, 0)}),
        Widget(f"hc{d}", 9, x + 1, 8, "HOTCUE", {n(27)}),
        Widget(f"pfx{d}", 9, x + 9, 7, "PADFX", {n(30)}),
        Widget(f"bj{d}", 9, x + 16, 6, "JUMP", {n(32)}),
        Widget(f"smp{d}", 9, x + 22, 8, "SAMPLE", {n(34)}),
        Widget(f"cue{d}", 12, x + 1, 7, "CUE", {n(12), n(72)}),
        Widget(f"play{d}", 12, x + 9, 8, "PLAY", {n(11), n(71)}),
    ]
    for k in range(8):
        row, col = (10, k) if k < 4 else (11, k - 4)
        ids = {_id("note", ch, mode * 16 + k) for ch in pad_channels for mode in range(8)}
        widgets.append(Widget(f"pad{d}_{k + 1}", row, x + 1 + col * 7, 7, str(k + 1), ids))
    return widgets


def _mixer() -> list[Widget]:
    x = MIXER_X
    widgets = [
        Widget("beatfx", 0, x + 4, 14, "BEAT FX ON", {_id("note", 5, 71)}),
        Widget("load1", 1, x, 6, "LOAD", {_id("note", 7, 70)}),
        Widget("browse", 1, x + 7, 8, "BROWSE", {_id("cc", 7, 64)}),
        Widget("load2", 1, x + 16, 6, "LOAD", {_id("note", 7, 71)}),
        Widget("master", 3, x + 7, 8, "MASTER", {_id("cc", 7, 8)}),
        Widget("phones", 4, x + 7, 8, "PHONES", {_id("cc", 7, 13)}),
        Widget("hpmix", 5, x + 7, 8, "HP MIX", {_id("cc", 7, 12)}),
        Widget("xfader", 12, x + 3, 16, "CROSSFADER", {_id("cc", 7, 31)}),
    ]
    for d, col in ((1, x), (2, x + 15)):
        for row, label, cc in ((3, "TRIM", 4), (4, "HI", 7), (5, "MID", 11), (6, "LOW", 15)):
            widgets.append(Widget(f"{label.lower()}{d}", row, col, 7, label, {_id("cc", d, cc)}))
        widgets.append(Widget(f"cfx{d}", 7, col, 7, "CFX", {_id("cc", 7, 22 + d)}))
        widgets.append(Widget(f"hp{d}", 8, col, 7, "CUE", {_id("note", d, 84)}))
        widgets.append(Widget(f"fader{d}", 10, col, 7, "FADER", {_id("cc", d, 19)}))
    return widgets


WIDGETS: list[Widget] = _deck(1) + _deck(2) + _mixer()
BY_ID: dict[str, Widget] = {i: w for w in WIDGETS for i in w.ids}

JOG_ART = [
    (3, "  .-~~~~~~~~~~~-.  "),
    (4, " /               \\ "),
    (5, "|                 |"),
    (6, " \\               / "),
    (7, "  '-~~~~~~~~~~~-'  "),
]


def _decorations() -> list[tuple[int, int, str]]:
    out = []
    for d, x in DECK_X.items():
        out += [(row, x + 7, art) for row, art in JOG_ART]
        rail = x + 3 if d == 1 else x + DECK_W - 4
        out += [(row, rail, "║") for row in range(3, 8)]
        out.append((2, x + 1, f"DECK {d}"))
    for col in (MIXER_X + 3, MIXER_X + 18):
        out.append((9, col, "║"))
    out.append((11, MIXER_X + 10, "╪"))
    return out


def widget_for(c: Control) -> Widget | None:
    c = coarse(c)
    return BY_ID.get(f"{c.kind}:{c.channel}:{c.number}")


def render(
    mapped: set[str] | None = None,
    pressed: Control | None = None,
    color: bool = False,
    title: str = " Pioneer DDJ-FLX4 ",
) -> str:
    """The controller drawing. `mapped` holds control ids ("note:1:11") that have a mapping."""
    mapped = mapped or set()
    grid = [[" "] * WIDTH for _ in range(ROWS)]
    styles: dict[tuple[int, int], tuple[str, int]] = {}  # (row, col) -> (style, length)

    def put(row: int, col: int, text: str) -> None:
        for i, ch in enumerate(text):
            if 0 <= col + i < WIDTH:
                grid[row][col + i] = ch

    for row, col, text in _decorations():
        put(row, col, text)
    hot = widget_for(pressed) if pressed else None
    for w in WIDGETS:
        state = "pressed" if w is hot else "mapped" if w.ids & mapped else "plain"
        left, right = {"pressed": ("»", "«"), "mapped": ("(", ")"), "plain": ("[", "]")}[state]
        put(w.row, w.col, left + w.label.center(w.width - 2)[: w.width - 2] + right)
        if color:
            styles[(w.row, w.col)] = ({"pressed": HILITE, "mapped": GREEN, "plain": DIM}[state], w.width)

    lines = []
    for r, cells in enumerate(grid):
        line = ""
        c = 0
        while c < WIDTH:
            if (r, c) in styles:
                style, length = styles[(r, c)]
                line += style + "".join(cells[c : c + length]) + RESET
                c += length
            else:
                line += cells[c]
                c += 1
        lines.append("│" + line + "│")
    top = "╭" + title.center(WIDTH, "─") + "╮"
    bottom = "╰" + "─" * WIDTH + "╯"
    return "\n".join([top, *lines, bottom])

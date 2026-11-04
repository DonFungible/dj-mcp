import re

from midi_mapper_flx4.config import Control
from midi_mapper_flx4.diagram import WIDGETS, WIDTH, render, widget_for
from midi_mapper_flx4.flx4 import coarse, control_name
from midi_mapper_flx4.service import pick_control


def test_layout_is_a_clean_box():
    lines = render().splitlines()
    assert {len(line) for line in lines} == {WIDTH + 2}
    spots = [(w.row, w.col + i) for w in WIDGETS for i in range(w.width)]
    assert len(spots) == len(set(spots)), "widgets overlap"


def test_states_keep_the_layout():
    plain = render()
    marked = render({"note:1:12"}, pressed=Control("note", 1, 11))
    assert "( CUE )" in marked and "» PLAY «" in marked
    assert len(plain) == len(marked)
    colored = render({"note:1:12"}, pressed=Control("note", 1, 11), color=True)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == marked


def test_controls_find_their_widget():
    assert widget_for(Control("note", 1, 11)).key == "play1"
    assert widget_for(Control("note", 2, 71)).key == "play2"  # shift + play lights the same button
    assert widget_for(Control("cc", 2, 34)).key == "jog2"
    assert widget_for(Control("note", 10, 0x23)).key == "pad2_4"  # beat jump mode, pad 4
    assert widget_for(Control("cc", 7, 63)).key == "xfader"  # crossfader, fine half
    assert widget_for(Control("note", 3, 1)) is None


def test_fine_and_coarse_halves_of_a_knob_are_one_control():
    assert coarse(Control("cc", 1, 51)) == Control("cc", 1, 19)
    assert coarse(Control("cc", 1, 33)) == Control("cc", 1, 33)  # jog wheel, not a fine half
    assert control_name(Control("cc", 1, 51)) == "Deck 1 Channel fader"
    assert control_name(Control("cc", 7, 31)) == "Crossfader"
    assert pick_control([Control("cc", 2, 39)]) == Control("cc", 2, 7)

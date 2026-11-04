import sys
from types import SimpleNamespace

import pytest

from midi_mapper_flx4.config import parse_combo
from midi_mapper_flx4.keycapture import combo, key_name


def named(name):
    return SimpleNamespace(name=name)


def char(c, vk=None):
    return SimpleNamespace(name=None, char=c, vk=vk)


def test_modifiers_and_special_keys():
    assert key_name(named("cmd_r")) == "cmd"
    assert key_name(named("alt_l")) == "alt"
    assert key_name(named("page_down")) == "pagedown"
    assert key_name(named("f12")) == "f12"
    assert key_name(named("caps_lock")) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS key codes")
def test_mac_key_codes_ignore_shift():
    assert key_name(char("}", vk=0x1E)) == "]"  # Shift+] still records the ] key
    assert key_name(char("P", vk=0x23)) == "p"


def test_typed_characters_fall_back_to_their_key():
    assert key_name(char("{")) == "["
    assert key_name(char("A")) == "a"
    assert key_name(char("+")) == "="  # shifted =, the key itself
    assert key_name(char("\x10")) is None  # a control character


def test_combos_are_ordered_and_valid():
    c = combo({"shift", "cmd"}, "]")
    assert c == "cmd+shift+]"
    assert parse_combo(c) == (("cmd", "shift"), "]")
    assert combo(set(), "enter") == "enter"

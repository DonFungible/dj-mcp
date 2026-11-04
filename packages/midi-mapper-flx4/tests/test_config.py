import pytest

from midi_mapper_flx4.config import ConfigError, Control, mapping_block, parse_combo, parse_config, starter_config


def test_parse_combo():
    assert parse_combo("cmd+shift+p") == (("cmd", "shift"), "p")
    assert parse_combo("Option + Return") == (("alt",), "enter")
    assert parse_combo("ctrl+plus") == (("ctrl",), "+")
    assert parse_combo("f5") == ((), "f5")
    for bad in ("cmd+", "hyper+a", "cmd+banana", ""):
        with pytest.raises(ConfigError):
            parse_combo(bad)


def test_starter_config_is_valid_and_empty():
    cfg = parse_config(starter_config())
    assert cfg.device == "DDJ-FLX4" and cfg.mappings == {}
    assert "rekordbox" in cfg.pause_when_running


def test_mappings_round_trip():
    text = starter_config()
    text += mapping_block(Control("note", 1, 11), {"keys": "cmd+c"}, "Deck 1 play")
    text += mapping_block(Control("note", 2, 11), {"text": 'say "hi"\n'})
    text += mapping_block(Control("cc", 1, 34), {"scroll": 3}, "jog")
    text += mapping_block(Control("cc", 2, 34), {"keys_cw": "down", "keys_ccw": "up"})
    cfg = parse_config(text)
    assert len(cfg.mappings) == 4
    assert cfg.mappings[Control("note", 1, 11)].keys == "cmd+c"
    assert cfg.mappings[Control("note", 2, 11)].text == 'say "hi"\n'
    assert cfg.mappings[Control("cc", 2, 34)].keys_ccw == "up"


@pytest.mark.parametrize(
    "body,err",
    [
        ('note = 1\nkeys = "a"\ncc = 2', "exactly one of note"),
        ('note = 1\nchannel = 17\nkeys = "a"', "channel"),
        ("note = 1", "exactly one action"),
        ('note = 1\nkeys = "a"\nshell = "x"', "exactly one action"),
        ("note = 1\nscroll = 3", "only works on a cc"),
        ('cc = 1\nkeys_cw = "down"', "together"),
        ('note = 1\nkeys = "cmd+banana"', "Unknown key"),
        ('cc = 1\nscroll = 3\nencoding = "weird"', "encoding"),
    ],
)
def test_invalid_mappings(body, err):
    with pytest.raises(ConfigError, match=err):
        parse_config(f"[[map]]\n{body}\n")


def test_duplicate_control_rejected():
    block = mapping_block(Control("note", 1, 5), {"keys": "a"})
    with pytest.raises(ConfigError, match="already mapped"):
        parse_config(block + block)

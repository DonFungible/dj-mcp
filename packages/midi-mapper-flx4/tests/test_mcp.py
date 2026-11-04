import threading
import time

import mido
import pytest
from conftest import VIRTUAL_MIDI
from mcp.server.mcpserver.exceptions import ToolError

from midi_mapper_flx4 import server, service
from midi_mapper_flx4.config import ConfigError, Control, load_config
from midi_mapper_flx4.flx4 import control_name, parse_control_id

DEVICE = "midi-mapper-flx4-mcp-test"


@pytest.mark.parametrize(
    "control, name",
    [
        (Control("note", 1, 11), "Deck 1 Play/Pause"),
        (Control("note", 2, 71), "Deck 2 Shift + Play/Pause"),
        (Control("cc", 1, 33), "Deck 1 Jog wheel (side)"),
        (Control("note", 8, 0), "Deck 1 Hot Cue pad 1"),
        (Control("note", 11, 0x27), "Deck 2 Shift + Beat Jump pad 8"),
        (Control("note", 1, 99), "Deck 1 button (note 99)"),
        (Control("note", 7, 3), "note 3 on channel 7"),
    ],
)
def test_control_names(control, name):
    assert control_name(control) == name


def test_control_ids():
    assert parse_control_id("note:1:11") == Control("note", 1, 11)
    for bad in ("note:1", "knob:1:2", "note:17:1", "cc:1:200", "x"):
        with pytest.raises(ConfigError):
            parse_control_id(bad)


def test_pick_control():
    touch, jog = Control("note", 1, 54), Control("cc", 1, 34)
    assert service.pick_control([touch, jog]) == jog  # turning the jog by its top starts with a touch
    assert service.pick_control([touch]) == touch
    msb, lsb = Control("cc", 7, 31), Control("cc", 7, 63)
    assert service.pick_control([lsb, msb]) == msb  # 14-bit fader: the MSB, whichever arrives first
    assert service.pick_control([Control("note", 1, 11), Control("note", 1, 12)]) == Control("note", 1, 12)


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'device = "{DEVICE}"\npause_when_running = []\n')
    monkeypatch.setattr(server, "config_path", cfg)
    # Never start a real background mapper from these tests unless a test asks for it.
    monkeypatch.setattr(server, "_ensure_mapper", lambda: {"mapper_pid": None, "mapper_started": False})
    return cfg


def test_map_list_unmap(mcp_env):
    res = server.map_button("note:1:11", "app", "Discord", name="Open Discord")
    assert res["saved"]["control_name"] == "Deck 1 Play/Pause" and res["replaced"] is None
    assert load_config(mcp_env).mappings[Control("note", 1, 11)].app == "Discord"

    res = server.map_button("note:1:11", "keys", "cmd+shift+m")
    assert res["replaced"]["action"] == {"type": "app", "value": "Discord"}

    server.map_button("cc:1:33", "turn", "down", counter_clockwise="up")
    assert [m["id"] for m in server.status()["mappings"]] == ["note:1:11", "cc:1:33"]

    assert server.unmap_button("note:1:11")["removed"]["id"] == "note:1:11"
    with pytest.raises(ToolError):
        server.unmap_button("note:1:11")


@pytest.mark.parametrize(
    "args",
    [("note:1:11", "keys", "cmd+banana"), ("note:1:11", "scroll", "3"), ("note:1:11", "app", " "), ("bad", "app", "X")],
)
def test_bad_mappings_rejected(mcp_env, args):
    before = mcp_env.read_text()
    with pytest.raises(ToolError):
        server.map_button(*args)
    assert mcp_env.read_text() == before


def test_test_mapping_runs_action(mcp_env, tmp_path):
    out = tmp_path / "ran.txt"
    server.map_button("note:2:12", "shell", f"echo hi > {out}")
    server.test_mapping("note:2:12")
    for _ in range(50):
        if out.exists():
            break
        time.sleep(0.1)
    assert out.read_text().strip() == "hi"


@pytest.mark.skipif(not VIRTUAL_MIDI, reason="virtual MIDI ports not supported here")
def test_wait_for_button(mcp_env):
    with mido.open_output(DEVICE, virtual=True) as port:

        def press():
            time.sleep(0.5)
            assert service.learning_active(mcp_env)  # the mapper is told to ignore presses meanwhile
            port.send(mido.Message("note_on", channel=0, note=54, velocity=127))  # touch...
            port.send(mido.Message("control_change", channel=0, control=34, value=65))  # ...and turn

        threading.Thread(target=press, daemon=True).start()
        res = server.wait_for_button(timeout_seconds=10)
    assert res["id"] == "cc:1:34" and res["kind"] == "jog wheel" and res["current_mapping"] is None
    assert "»  JOG  «" in res["diagram"]
    assert not service.learning_active(mcp_env)


@pytest.mark.skipif(not VIRTUAL_MIDI, reason="virtual MIDI ports not supported here")
def test_wait_for_button_times_out(mcp_env):
    with mido.open_output(DEVICE, virtual=True), pytest.raises(ToolError, match="No button"):
        server.wait_for_button(timeout_seconds=1)  # clamped to 5 s


def test_not_connected(mcp_env):
    with pytest.raises(ToolError, match="isn't connected"):
        server.wait_for_button()


def test_background_mapper_start_stop(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'device = "{DEVICE}"\npause_when_running = []\n')
    pid = service.start_background(cfg)
    try:
        assert service.running_pid(cfg) == pid
        assert service.start_background(cfg) == pid  # idempotent
        from midi_mapper_flx4.runtime import Runtime

        with pytest.raises(RuntimeError, match="already running"):  # one mapper at a time
            Runtime(cfg, verbose=False).start()
    finally:
        assert service.stop_background(cfg) == pid
    assert service.running_pid(cfg) is None and not service.pid_file(cfg).exists()


def test_no_midi_system(mcp_env, monkeypatch):
    """Linux without ALSA: report "not connected" instead of crashing."""

    def boom():
        raise RuntimeError("MidiInAlsa::initialize: error creating ALSA sequencer client object.")

    monkeypatch.setattr(mido, "get_input_names", boom)
    assert server.status()["connected"] is False
    with pytest.raises(ToolError, match="isn't connected"):
        server.wait_for_button()

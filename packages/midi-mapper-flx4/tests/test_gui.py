import http.client
import json
import threading
import time

import mido
import pytest
from conftest import VIRTUAL_MIDI

from midi_mapper_flx4 import engine
from midi_mapper_flx4.config import (
    Control,
    mapping_block,
    mapping_from_json,
    mapping_to_json,
    parse_config,
    starter_config,
)
from midi_mapper_flx4.gui import GuiServer
from midi_mapper_flx4.runtime import Runtime

DEVICE = "midi-mapper-flx4-gui-test"


def test_json_round_trip():
    text = starter_config()
    text += mapping_block(Control("note", 1, 11), {"keys": "cmd+c"}, "copy")
    text += mapping_block(Control("cc", 2, 34), {"keys_cw": "down", "keys_ccw": "up"})
    text += mapping_block(Control("cc", 1, 33), {"scroll": -2, "encoding": "twos"})
    for m in parse_config(text).mappings.values():
        assert mapping_from_json(mapping_to_json(m)) == m


@pytest.mark.parametrize(
    "action",
    [
        {"type": "keys", "value": ""},
        {"type": "keys", "value": "cmd+banana"},
        {"type": "scroll", "value": "x"},
        {"type": "turn", "cw": "down", "ccw": ""},
        {"type": "launch-rockets"},
    ],
)
def test_bad_json_rejected(action):
    from midi_mapper_flx4.config import ConfigError

    with pytest.raises(ConfigError):
        mapping_from_json({"control": {"kind": "cc", "channel": 1, "number": 1}, "action": action})


@pytest.fixture
def gui(tmp_path, monkeypatch):
    if not VIRTUAL_MIDI:
        pytest.skip("virtual MIDI ports not supported here")
    monkeypatch.setattr(engine, "SHELL_LOG", tmp_path / "shell.log")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'device = "{DEVICE}"\npause_when_running = []\n')
    out = mido.open_output(DEVICE, virtual=True)
    rt = Runtime(cfg, verbose=False)
    server = GuiServer(rt)
    rt.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(50):
        if rt.port_name:
            break
        time.sleep(0.1)
    assert rt.port_name, "runtime never connected to the virtual port"
    yield server, out, cfg, tmp_path
    server.shutdown()
    rt.stop()
    out.close()


def request(server, method, path, body=None, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{server.server_port}"}
    if token:
        headers["X-Token"] = server.token if token is True else token
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_security(gui):
    server, *_ = gui
    assert request(server, "GET", "/api/state", token=False)[0] == 403
    assert request(server, "GET", "/api/state", token="wrong")[0] == 403
    assert request(server, "GET", "/api/state", host="evil.example:80")[0] == 403
    assert request(server, "GET", "/", host="evil.example:80")[0] == 403
    # token in the query string is accepted for the read-only event stream only, never for writes
    status, _ = request(server, "POST", f"/api/enabled?token={server.token}", {"enabled": False}, token=False)
    assert status == 403
    status, page = request(server, "GET", "/", token=False)
    assert status == 200 and b"midi-mapper-flx4" in page


def test_save_fire_and_delete(gui):
    server, out, cfg, tmp = gui
    marker = tmp / "fired"
    ctrl = {"kind": "note", "channel": 3, "number": 42}
    status, state = request(
        server,
        "POST",
        "/api/mappings",
        {"control": ctrl, "name": "touch it", "action": {"type": "shell", "value": f"touch {marker}"}},
    )
    assert status == 200, state
    assert [m["name"] for m in state["mappings"]] == ["touch it"]
    assert "touch it" in cfg.read_text()

    out.send(mido.Message("note_on", channel=2, note=42, velocity=127))
    assert wait_for(marker.exists)

    status, state = request(server, "POST", "/api/mappings/delete", {"control": ctrl})
    assert status == 200 and state["mappings"] == []
    assert parse_config(cfg.read_text()).mappings == {}


def test_invalid_mapping_leaves_config_untouched(gui):
    server, _, cfg, _ = gui
    before = cfg.read_text()
    status, body = request(
        server,
        "POST",
        "/api/mappings",
        {"control": {"kind": "note", "channel": 1, "number": 1}, "action": {"type": "scroll", "value": 3}},
    )
    assert status == 400 and "cc control" in body["error"]
    assert cfg.read_text() == before


def test_replace_moves_mapping(gui):
    server, *_ = gui
    a = {"kind": "note", "channel": 1, "number": 1}
    b = {"kind": "note", "channel": 1, "number": 2}
    request(server, "POST", "/api/mappings", {"control": a, "action": {"type": "keys", "value": "esc"}})
    _, state = request(
        server, "POST", "/api/mappings", {"control": b, "replace": a, "action": {"type": "keys", "value": "esc"}}
    )
    assert [m["control"]["number"] for m in state["mappings"]] == [2]


def test_learning_suppresses_actions_and_emits_events(gui):
    server, out, _, tmp = gui
    marker = tmp / "should-not-exist"
    ctrl = {"kind": "note", "channel": 1, "number": 7}
    request(server, "POST", "/api/mappings", {"control": ctrl, "action": {"type": "shell", "value": f"touch {marker}"}})
    q = server.runtime.subscribe()
    _, state = request(server, "POST", "/api/learn", {"active": True})
    assert state["learning"]
    out.send(mido.Message("note_on", channel=0, note=7, velocity=127))
    ev = None
    end = time.time() + 5
    while time.time() < end:
        e = q.get(timeout=5)
        if e["type"] == "learn":
            ev = e
            break
    assert ev and ev["control"] == ctrl and ev["mapped"]
    time.sleep(0.5)
    assert not marker.exists(), "action fired while learning"
    _, state = request(server, "POST", "/api/learn", {"active": False})
    assert not state["learning"]


def test_disabled_does_not_fire(gui):
    server, out, _, tmp = gui
    marker = tmp / "off"
    request(
        server,
        "POST",
        "/api/mappings",
        {
            "control": {"kind": "note", "channel": 1, "number": 9},
            "action": {"type": "shell", "value": f"touch {marker}"},
        },
    )
    request(server, "POST", "/api/enabled", {"enabled": False})
    out.send(mido.Message("note_on", channel=0, note=9, velocity=127))
    time.sleep(0.5)
    assert not marker.exists()


def test_learning_file_from_mcp_suppresses_actions(gui):
    from midi_mapper_flx4 import service

    server, out, cfg, tmp = gui
    request(
        server,
        "POST",
        "/api/mappings",
        {
            "control": {"kind": "note", "channel": 1, "number": 11},
            "action": {"type": "shell", "value": f"echo fired >> {tmp / 'fired.txt'}"},
        },
    )
    service.learn_file(cfg).write_text(str(time.time() + 30))
    out.send(mido.Message("note_on", channel=0, note=11, velocity=127))
    time.sleep(0.5)
    assert not (tmp / "fired.txt").exists()
    service.learn_file(cfg).unlink()
    out.send(mido.Message("note_on", channel=0, note=11, velocity=127))
    assert wait_for(lambda: (tmp / "fired.txt").exists())


def test_state_has_flx4_names(gui):
    server, *_ = gui
    request(
        server,
        "POST",
        "/api/mappings",
        {"control": {"kind": "note", "channel": 1, "number": 11}, "action": {"type": "app", "value": "Discord"}},
    )
    m = server.runtime.state()["mappings"][0]
    assert m["control_name"] == "Deck 1 Play/Pause" and m["id"] == "note:1:11"
    assert m["action"] == {"type": "app", "value": "Discord"}

import threading
import time

import mido
import psutil
import pytest
from conftest import needs_virtual_midi

from midi_mapper_flx4.config import Control, mapping_block, parse_config
from midi_mapper_flx4.engine import Executor, Mapper, PauseWatcher, _proc_name, relative_delta


class Recorder(Executor):
    def __init__(self):
        super().__init__()
        self.calls = []

    def keys(self, combo, times=1):
        self.calls.append(("keys", combo, times))

    def text(self, text):
        self.calls.append(("text", text))

    def shell(self, command):
        self.calls.append(("shell", command))

    def scroll(self, lines):
        self.calls.append(("scroll", lines))

    def app(self, name):
        self.calls.append(("app", name))


@pytest.fixture
def mapper():
    text = (
        mapping_block(Control("note", 1, 11), {"keys": "cmd+c"})
        + mapping_block(Control("note", 2, 11), {"shell": "true"})
        + mapping_block(Control("note", 1, 12), {"text": "hi\n"})
        + mapping_block(Control("cc", 1, 34), {"scroll": 3})
        + mapping_block(Control("cc", 2, 34), {"keys_cw": "down", "keys_ccw": "up"})
        + mapping_block(Control("cc", 7, 5), {"keys": "esc"})
        + mapping_block(Control("note", 8, 0), {"app": "Discord"})
    )
    return Mapper(parse_config(text), Recorder())


def calls(m):
    return m.executor.calls


def test_relative_delta():
    assert relative_delta(65, "offset64") == 1
    assert relative_delta(60, "offset64") == -4
    assert relative_delta(3, "twos") == 3
    assert relative_delta(127, "twos") == -1


def test_buttons(mapper):
    mapper.handle(mido.Message("note_on", channel=0, note=11, velocity=127))
    mapper.handle(mido.Message("note_on", channel=0, note=11, velocity=0))  # release: ignored
    mapper.handle(mido.Message("note_off", channel=0, note=11))  # ignored
    mapper.handle(mido.Message("note_on", channel=1, note=11, velocity=127))
    mapper.handle(mido.Message("note_on", channel=0, note=12, velocity=100))
    mapper.handle(mido.Message("note_on", channel=0, note=99, velocity=127))  # unmapped
    assert calls(mapper) == [("keys", "cmd+c", 1), ("shell", "true"), ("text", "hi\n")]


def test_encoders(mapper):
    mapper.handle(mido.Message("control_change", channel=0, control=34, value=65))
    mapper.handle(mido.Message("control_change", channel=0, control=34, value=62))
    mapper.handle(mido.Message("control_change", channel=0, control=34, value=64))  # no movement
    assert calls(mapper) == [("scroll", 3), ("scroll", -6)]  # scrolling: every tick counts


def test_turn_mappings_need_real_movement_per_step(mapper, monkeypatch):
    from midi_mapper_flx4 import engine

    def turn(value, times=1):
        for _ in range(times):
            mapper.handle(mido.Message("control_change", channel=1, control=34, value=value))

    turn(65, 23)  # 23 ticks clockwise: not a full step yet
    assert calls(mapper) == []
    turn(65)  # the 24th tick
    assert calls(mapper) == [("keys", "down", 1)]
    turn(63, 30)  # reversing starts counting again: 30 ticks back is one step
    assert calls(mapper)[1:] == [("keys", "up", 1)]
    turn(127)  # one big flick of 63 ticks clockwise
    assert calls(mapper)[2:] == [("keys", "down", 2)]
    clock = [engine.time.monotonic() + 100]  # well after the flick above
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    turn(65, 20)
    clock[0] += 5  # a pause: leftover movement doesn't count towards the next step
    turn(65, 20)
    assert len(calls(mapper)) == 3


def test_ticks_can_be_set_per_mapping():
    from midi_mapper_flx4.config import mapping_block, parse_config

    m = Mapper(
        parse_config(mapping_block(Control("cc", 2, 33), {"keys_cw": "right", "keys_ccw": "left", "ticks": 4})),
        Recorder(),
    )
    for _ in range(8):
        m.handle(mido.Message("control_change", channel=1, control=33, value=65))
    assert calls(m) == [("keys", "right", 1), ("keys", "right", 1)]


def test_cc_button_fires_only_at_full(mapper):
    mapper.handle(mido.Message("control_change", channel=6, control=5, value=40))
    mapper.handle(mido.Message("control_change", channel=6, control=5, value=127))
    assert calls(mapper) == [("keys", "esc", 1)]


def test_pause_watcher_detects_process():
    me = psutil.Process().name()
    assert PauseWatcher([me.upper()], interval=3600).paused
    assert not PauseWatcher(["definitely-not-running-xyz"], interval=3600).paused


@needs_virtual_midi
def test_end_to_end_over_virtual_midi(tmp_path, monkeypatch):
    """Real MIDI through a virtual port into `midi-mapper-flx4 run`, using a shell action only."""
    from midi_mapper_flx4 import cli, engine

    monkeypatch.setattr(engine, "SHELL_LOG", tmp_path / "shell.log")
    marker = tmp_path / "pressed"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'device = "midi-mapper-flx4-test"\npause_when_running = []\n'
        + mapping_block(Control("note", 3, 42), {"shell": f"touch {marker}"})
    )
    with mido.open_output("midi-mapper-flx4-test", virtual=True) as out:
        threading.Thread(target=cli.main, args=(["--config", str(cfg), "run"],), daemon=True).start()
        time.sleep(1.0)
        out.send(mido.Message("note_on", channel=2, note=42, velocity=127))
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
    assert marker.exists()


def test_process_names_ignore_case_and_exe():
    assert _proc_name("rekordbox.exe") == "rekordbox"
    assert _proc_name("Serato DJ Pro.EXE") == "serato dj pro"
    assert _proc_name("rekordboxAgent") == "rekordboxagent"  # helper process must not count as the app


def test_app_action(mapper):
    mapper.handle(mido.Message("note_on", channel=7, note=0, velocity=127))
    assert calls(mapper) == [("app", "Discord")]


def test_app_command():
    import sys

    from midi_mapper_flx4.engine import app_command

    cmd = app_command("Discord")
    assert cmd[-1] == ("discord" if sys.platform not in ("darwin", "win32") else "Discord")


def test_pad_mappings_work_in_every_pad_mode():
    from midi_mapper_flx4.config import mapping_block, parse_config

    text = mapping_block(Control("note", 10, 49), {"app": "Linear"})  # deck 2 Sampler pad 2
    text += mapping_block(Control("note", 10, 3), {"app": "Notes"})  # deck 2 Hot Cue pad 4, mapped on its own
    text += mapping_block(Control("note", 10, 51), {"app": "Mail"})  # deck 2 Sampler pad 4
    m = Mapper(parse_config(text), Recorder())
    for note in (49, 1, 17):  # Sampler, Hot Cue and Pad FX mode: pad 2 every time
        m.handle(mido.Message("note_on", channel=9, note=note, velocity=127))
    m.handle(mido.Message("note_on", channel=9, note=3, velocity=127))  # Hot Cue pad 4 has its own mapping
    m.handle(mido.Message("note_on", channel=9, note=19, velocity=127))  # Pad FX pad 4: the first mapping found
    m.handle(mido.Message("note_on", channel=7, note=1, velocity=127))  # deck 1 pads are separate
    assert calls(m) == [("app", "Linear")] * 3 + [("app", "Notes"), ("app", "Notes")]

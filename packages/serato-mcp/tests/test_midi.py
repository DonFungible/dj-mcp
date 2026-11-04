import asyncio
import sys
import time

import pytest

from serato_mcp import coremidi
from serato_mcp.midi import PORT_NAME, bridge, message_for, perform, sweep, tempo_value

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="CoreMIDI is macOS only")


def test_virtual_device_loopback_and_feedback():
    async def scenario() -> list[list[int]]:
        bridge.open()
        time.sleep(0.3)
        assert PORT_NAME in coremidi.source_names(), "other MIDI apps should see the device"
        got: list[list[int]] = []
        listener = coremidi.Client("serato-mcp-test")
        listener.listen(PORT_NAME, got.append)
        await perform("play_pause", 2)
        await perform("crossfader", None, 127)
        await sweep("filter", 1, 64, 0, 100)
        # Serato's LED feedback arrives on our virtual destination.
        port = listener.output_port()
        listener.send(port, coremidi.destination_names().index(PORT_NAME), bytes([0x90, 11, 127]))
        await asyncio.sleep(0.3)
        return got

    try:
        got = asyncio.run(scenario())
        assert got[0] == [0x91, 0, 127]  # note on, channel 2
        assert got[1] == [0x81, 0, 0]  # note off
        assert got[2] == [0xBF, 8, 127]  # crossfader CC 8, channel 16
        assert all(m[0] == 0xB0 and m[1] == 19 for m in got[3:]), "filter sweep on deck 1"
        assert got[-1] == [0xB0, 19, 0]
        assert bridge.received()[-1]["bytes"] == [0x90, 11, 127]
    finally:
        bridge.close()
    time.sleep(0.2)
    assert PORT_NAME not in coremidi.source_names()


def test_messages_and_tempo_values():
    assert message_for("hotcue_3", 1)["bytes"] == [0x90, 12, 127]
    assert message_for("volume", 4, 200)["bytes"] == [0xB3, 7, 127]
    with pytest.raises(ValueError):
        message_for("play_pause")  # deck actions need a deck
    assert tempo_value(0) == 64 and tempo_value(8) == 127 and tempo_value(-8) == 0 and tempo_value(50, 8) == 127

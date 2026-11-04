import pytest


def _virtual_midi_supported() -> bool:
    """Virtual MIDI ports work on macOS (CoreMIDI) and Linux with ALSA sequencer, not on Windows."""
    try:
        import mido

        port = mido.open_output("midi-mapper-flx4-probe", virtual=True)
        port.close()
        return True
    except Exception:  # noqa: BLE001 - any backend failure means "unsupported here"
        return False


VIRTUAL_MIDI = _virtual_midi_supported()
needs_virtual_midi = pytest.mark.skipif(not VIRTUAL_MIDI, reason="virtual MIDI ports not supported here")

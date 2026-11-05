"""Keep macOS's MIDI device list current in long-running processes.

CoreMIDI only updates a process's view of the devices (plugged in, unplugged, reset by DJ software) through
notifications delivered on a run loop. The mapper has no run loop, so without this it kept seeing a controller
that had been replugged or reset, and listened on a connection that delivered nothing. Starting one CoreMIDI
client on a thread that runs a CFRunLoop, before anything else touches MIDI, fixes that for the whole process.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
import time

_started = False
_lock = threading.Lock()


def _run() -> None:
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    midi = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreMIDI"))
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    midi.MIDIClientCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    client = ctypes.c_uint32()
    name = cf.CFStringCreateWithCString(None, b"midi-mapper-flx4 device watch", 0x08000100)
    midi.MIDIClientCreate(name, None, None, ctypes.byref(client))
    cf.CFRunLoopRun()  # delivers CoreMIDI's device notifications for this process; never returns


def start() -> None:
    """Start watching (macOS only, once per process). Call before the first MIDI call."""
    global _started
    if sys.platform != "darwin":
        return
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_run, name="coremidi-watch", daemon=True).start()
        time.sleep(0.2)  # let the client exist before rtmidi creates its own

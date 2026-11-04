"""Minimal CoreMIDI bindings (macOS) through ctypes: virtual source/destination, sending, and port lists.

Using the system framework directly means serato-mcp has no compiled dependencies, so it installs on any
Python version without a compiler.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import struct
import threading
from collections.abc import Callable

_UTF8 = 0x08000100  # kCFStringEncodingUTF8

_cf = None
_midi = None


class MIDIError(RuntimeError):
    pass


def _libs():
    global _cf, _midi
    if _midi is None:
        cf_path = ctypes.util.find_library("CoreFoundation")
        midi_path = ctypes.util.find_library("CoreMIDI")
        if not cf_path or not midi_path:
            raise MIDIError("CoreMIDI is only available on macOS")
        cf = ctypes.cdll.LoadLibrary(cf_path)
        midi = ctypes.cdll.LoadLibrary(midi_path)
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        u32p = ctypes.POINTER(ctypes.c_uint32)
        for name, args in {
            "MIDIClientCreate": [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, u32p],
            "MIDISourceCreate": [ctypes.c_uint32, ctypes.c_void_p, u32p],
            "MIDIDestinationCreate": [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, u32p],
            "MIDIOutputPortCreate": [ctypes.c_uint32, ctypes.c_void_p, u32p],
            "MIDIInputPortCreate": [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, u32p],
            "MIDIPortConnectSource": [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            "MIDIReceived": [ctypes.c_uint32, ctypes.c_void_p],
            "MIDISend": [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            "MIDIEndpointDispose": [ctypes.c_uint32],
            "MIDIPortDispose": [ctypes.c_uint32],
            "MIDIClientDispose": [ctypes.c_uint32],
            "MIDIObjectGetStringProperty": [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        }.items():
            fn = getattr(midi, name)
            fn.argtypes = args
            fn.restype = ctypes.c_int32
        for name in ("MIDIGetNumberOfSources", "MIDIGetNumberOfDestinations"):
            getattr(midi, name).restype = ctypes.c_ulong
        for name in ("MIDIGetSource", "MIDIGetDestination"):
            getattr(midi, name).restype = ctypes.c_uint32
            getattr(midi, name).argtypes = [ctypes.c_ulong]
        _cf, _midi = cf, midi
    return _cf, _midi


def _cfstr(text: str) -> int:
    cf, _ = _libs()
    return cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)


def _check(status: int, what: str) -> None:
    if status != 0:
        raise MIDIError(f"{what} failed (OSStatus {status})")


def _name(obj: int) -> str:
    cf, midi = _libs()
    key = ctypes.c_void_p.in_dll(midi, "kMIDIPropertyName")
    out = ctypes.c_void_p()
    if midi.MIDIObjectGetStringProperty(obj, key, ctypes.byref(out)) != 0 or not out.value:
        return ""
    buf = ctypes.create_string_buffer(1024)
    ok = cf.CFStringGetCString(out, buf, len(buf), _UTF8)
    cf.CFRelease(out)
    return buf.value.decode("utf-8") if ok else ""


def source_names() -> list[str]:
    """MIDI sources: what other apps call inputs."""
    _, midi = _libs()
    return [_name(midi.MIDIGetSource(i)) for i in range(midi.MIDIGetNumberOfSources())]


def destination_names() -> list[str]:
    """MIDI destinations: what other apps call outputs."""
    _, midi = _libs()
    return [_name(midi.MIDIGetDestination(i)) for i in range(midi.MIDIGetNumberOfDestinations())]


def _packet_list(data: bytes) -> ctypes.Array:
    """A MIDIPacketList holding one packet with timestamp 0 (now)."""
    raw = struct.pack("<I", 1) + struct.pack("<QH", 0, len(data)) + data
    return ctypes.create_string_buffer(raw, max(len(raw), 64))


def _parse_packets(ptr: int) -> list[bytes]:
    """Messages in a MIDIPacketList (packets are 4-byte aligned on arm64)."""
    (count,) = struct.unpack("<I", ctypes.string_at(ptr, 4))
    out = []
    offset = 4
    for _ in range(count):
        _, length = struct.unpack("<QH", ctypes.string_at(ptr + offset, 10))
        out.append(ctypes.string_at(ptr + offset + 10, length))
        offset += 10 + length
        if platform.machine() == "arm64":
            offset = (offset + 3) & ~3
    return out


_READ_PROC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _split(data: bytes) -> list[list[int]]:
    """Split a packet's bytes into individual channel messages."""
    msgs, i = [], 0
    while i < len(data):
        status = data[i]
        size = 2 if status & 0xF0 in (0xC0, 0xD0) else 1 if status >= 0xF8 else 3
        msgs.append(list(data[i : i + size]))
        i += size
    return msgs


class Client:
    """One CoreMIDI client with the ports serato-mcp needs."""

    def __init__(self, name: str):
        _, midi = _libs()
        self._midi = midi
        self.client = ctypes.c_uint32()
        name_ref = _cfstr(name)
        _check(midi.MIDIClientCreate(name_ref, None, None, ctypes.byref(self.client)), "MIDIClientCreate")
        self._procs: list = []  # keep callbacks alive
        self._lock = threading.Lock()

    def virtual_source(self, name: str) -> int:
        """A virtual source other apps see as an input called `name`."""
        ep = ctypes.c_uint32()
        _check(self._midi.MIDISourceCreate(self.client, _cfstr(name), ctypes.byref(ep)), "MIDISourceCreate")
        return ep.value

    def virtual_destination(self, name: str, on_message: Callable[[list[int]], None]) -> int:
        """A virtual destination other apps see as an output called `name`."""
        proc = _READ_PROC(lambda plist, _ref, _src: [on_message(m) for d in _parse_packets(plist) for m in _split(d)])
        self._procs.append(proc)
        ep = ctypes.c_uint32()
        status = self._midi.MIDIDestinationCreate(self.client, _cfstr(name), proc, None, ctypes.byref(ep))
        _check(status, "MIDIDestinationCreate")
        return ep.value

    def output_port(self) -> int:
        port = ctypes.c_uint32()
        _check(self._midi.MIDIOutputPortCreate(self.client, _cfstr("out"), ctypes.byref(port)), "MIDIOutputPortCreate")
        return port.value

    def listen(self, source_name: str, on_message: Callable[[list[int]], None]) -> int:
        """Connect an input port to the first source whose name contains `source_name`."""
        idx = next((i for i, n in enumerate(source_names()) if source_name in n), None)
        if idx is None:
            raise MIDIError(f"No MIDI source named {source_name!r}")
        proc = _READ_PROC(lambda plist, _ref, _src: [on_message(m) for d in _parse_packets(plist) for m in _split(d)])
        self._procs.append(proc)
        port = ctypes.c_uint32()
        _check(
            self._midi.MIDIInputPortCreate(self.client, _cfstr("in"), proc, None, ctypes.byref(port)),
            "MIDIInputPortCreate",
        )
        _check(self._midi.MIDIPortConnectSource(port, self._midi.MIDIGetSource(idx), None), "MIDIPortConnectSource")
        return port.value

    def received(self, source: int, data: bytes) -> None:
        """Send from one of our virtual sources."""
        with self._lock:
            _check(self._midi.MIDIReceived(source, _packet_list(data)), "MIDIReceived")

    def send(self, port: int, destination_index: int, data: bytes) -> None:
        """Send through an output port to an existing destination (e.g. an IAC bus)."""
        with self._lock:
            dest = self._midi.MIDIGetDestination(destination_index)
            _check(self._midi.MIDISend(port, dest, _packet_list(data)), "MIDISend")

    def dispose_endpoint(self, endpoint: int) -> None:
        self._midi.MIDIEndpointDispose(endpoint)

    def dispose_port(self, port: int) -> None:
        self._midi.MIDIPortDispose(port)

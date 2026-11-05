import sys
import types

import pytest

from midi_mapper_flx4 import permissions


@pytest.fixture
def fake_mac(monkeypatch):
    calls = []
    state = {"trusted": False, "listen": False}
    hi = types.SimpleNamespace(
        AXIsProcessTrusted=lambda: state["trusted"],
        AXIsProcessTrustedWithOptions=lambda opts: calls.append(("prompt", "accessibility")) or state["trusted"],
        kAXTrustedCheckOptionPrompt="AXTrustedCheckOptionPrompt",
    )
    quartz = types.SimpleNamespace(
        CGPreflightListenEventAccess=lambda: state["listen"],
        CGRequestListenEventAccess=lambda: calls.append(("prompt", "input_monitoring")) or state["listen"],
    )
    monkeypatch.setitem(sys.modules, "HIServices", hi)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(permissions, "_mac", lambda: True)
    monkeypatch.setattr(permissions, "_asked", set())
    monkeypatch.setattr(permissions, "open_settings", lambda which: calls.append(("settings", which)))
    return calls, state


def test_prompts_once_then_opens_settings(fake_mac):
    calls, _ = fake_mac
    assert permissions.request_send_keys() is False
    assert permissions.request_send_keys() is False
    assert calls == [("prompt", "accessibility"), ("settings", "accessibility")]
    permissions.request_listen()
    permissions.request_listen()
    assert calls[2:] == [("prompt", "input_monitoring"), ("settings", "input_monitoring")]


def test_nothing_happens_when_already_allowed(fake_mac):
    calls, state = fake_mac
    state.update(trusted=True, listen=True)
    assert permissions.request_send_keys() is True and permissions.request_listen() is True
    assert calls == []


def test_other_systems_need_nothing(monkeypatch):
    monkeypatch.setattr(permissions, "_mac", lambda: False)
    assert permissions.can_send_keys() is None and permissions.request_send_keys() is None

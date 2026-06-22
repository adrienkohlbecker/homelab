"""Tests for resolve_net_backend's env-override + capability-probe logic.

The probe itself (_passt_available) execs qemu and reads platform/PATH, so
these patch it out and exercise the decision table around it -- the part that
decides whether a given environment (CI noble image vs jammy host vs macOS)
ends up on passt or slirp.
"""

import machine
import pytest


def test_override_slirp_pins_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "slirp")
    # Even where passt is available, the explicit override wins.
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: True)
    assert machine.resolve_net_backend("qemu-system-x86_64") == "slirp"


def test_auto_follows_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_NET_BACKEND", raising=False)
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: True)
    assert machine.resolve_net_backend("q") == "passt"
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: False)
    assert machine.resolve_net_backend("q") == "slirp"


def test_override_passt_errors_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing passt on a host that can't run it fails loudly rather than
    silently degrading to slirp -- a misconfigured CI env should surface."""
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "passt")
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: False)
    with pytest.raises(RuntimeError, match="passt is unusable"):
        machine.resolve_net_backend("qemu-system-x86_64")


def test_override_passt_honoured_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "passt")
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: True)
    assert machine.resolve_net_backend("q") == "passt"


def test_invalid_override_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "bogus")
    with pytest.raises(RuntimeError, match="not in auto/slirp/passt"):
        machine.resolve_net_backend("q")


def test_passt_unavailable_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe short-circuits to False off Linux (passt is Linux-only),
    before any PATH lookup or qemu exec."""
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    # which/subprocess must not even be consulted; make them blow up if they are.
    monkeypatch.setattr(machine.shutil, "which", lambda _n: pytest.fail("which called off Linux"))
    machine._passt_available.cache_clear()
    assert machine._passt_available("qemu-system-x86_64") is False
    machine._passt_available.cache_clear()

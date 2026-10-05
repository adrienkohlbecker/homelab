"""Tests for resolve_net_backend's env-override + capability-probe logic.

The probe itself (_passt_available) execs qemu and reads platform/PATH, so
these patch it out and exercise the decision table around it -- the part that
decides whether a given environment supports passt or must fall back to slirp
ends up on passt or slirp.
"""

import subprocess

import machine
import pytest
from arch import AARCH64, X86_64


def test_override_slirp_pins_legacy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "slirp")
    # Even where passt is available, the explicit override wins.
    monkeypatch.setattr(machine, "_passt_available", lambda _qb, _mt: True)
    assert machine.resolve_net_backend(X86_64) == "slirp"


def test_auto_follows_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_NET_BACKEND", raising=False)
    monkeypatch.setattr(machine, "_passt_available", lambda _qb, _mt: True)
    assert machine.resolve_net_backend(X86_64) == "passt"
    monkeypatch.setattr(machine, "_passt_available", lambda _qb, _mt: False)
    assert machine.resolve_net_backend(X86_64) == "slirp"


def test_override_passt_errors_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing passt on a host that can't run it fails loudly rather than
    silently degrading to slirp -- a misconfigured CI env should surface."""
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "passt")
    monkeypatch.setattr(machine, "_passt_available", lambda _qb, _mt: False)
    with pytest.raises(RuntimeError, match="passt is unusable"):
        machine.resolve_net_backend(X86_64)


def test_override_passt_honoured_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "passt")
    monkeypatch.setattr(machine, "_passt_available", lambda _qb, _mt: True)
    assert machine.resolve_net_backend(X86_64) == "passt"


def test_invalid_override_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "bogus")
    with pytest.raises(RuntimeError, match="not in auto/slirp/passt"):
        machine.resolve_net_backend(X86_64)


def test_passt_unavailable_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe short-circuits to False off Linux (passt is Linux-only),
    before any PATH lookup or qemu exec."""
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    # which/subprocess must not even be consulted; make them blow up if they are.
    monkeypatch.setattr(machine.shutil, "which", lambda _n: pytest.fail("which called off Linux"))
    machine._passt_available.cache_clear()
    assert machine._passt_available("qemu-system-x86_64", "q35") is False
    machine._passt_available.cache_clear()


def _fake_aarch64_qemu(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Mimic qemu-system-aarch64, which has no default machine: `-netdev help`
    only lists the netdev types once a machine is named."""
    if "-machine" not in argv:
        return subprocess.CompletedProcess(
            argv, 1, "", "qemu-system-aarch64: No machine specified, and there is no default\n"
        )
    return subprocess.CompletedProcess(argv, 0, "Available netdev backend types:\nsocket\nstream\n", "")


@pytest.mark.parametrize("override", ["auto", "passt"])
def test_aarch64_probe_names_the_machine(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    """An aarch64 Linux host with passt resolves to passt: the probe passes
    the arch's machine type, without which aarch64 qemu refuses `-netdev help`."""
    monkeypatch.setenv("HOMELAB_NET_BACKEND", override)
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(machine.subprocess, "run", _fake_aarch64_qemu)
    machine._passt_available.cache_clear()
    try:
        assert machine.resolve_net_backend(AARCH64) == "passt"
    finally:
        machine._passt_available.cache_clear()

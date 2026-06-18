"""Tests for resolve_net_backend's env-override + capability-probe logic.

The probe itself (_passt_available) execs qemu and reads platform/PATH, so
these patch it out and exercise the decision table around it -- the part that
decides whether a given environment (CI noble image vs jammy host vs macOS)
ends up on passt or slirp.
"""

import pytest

import machine


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


# --- native passt netdev (qemu >= 10.1) gating -----------------------------


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("QEMU emulator version 9.2.3\n", (9, 2)),
        ("QEMU emulator version 10.1.0\n", (10, 1)),
        ("QEMU emulator version 11.0.1 (v11.0.1)\n", (11, 0)),
        # Unparseable / empty output falls back to the (0, 0) sentinel, which
        # every >= gate treats as "too old" -> legacy path.
        ("totally unexpected\n", (0, 0)),
        ("", (0, 0)),
    ],
)
def test_qemu_version_parses_major_minor(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: tuple[int, int]
) -> None:
    monkeypatch.setattr(machine.subprocess, "run", lambda *a, **k: _FakeProc(stdout=output))
    machine._qemu_version.cache_clear()
    assert machine._qemu_version("qemu-system-x86_64") == expected
    machine._qemu_version.cache_clear()


def test_qemu_version_sentinel_on_exec_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary that can't be run yields (0, 0), not an exception -- the gate
    degrades to the legacy path instead of crashing the harness."""

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no such binary")

    monkeypatch.setattr(machine.subprocess, "run", _boom)
    machine._qemu_version.cache_clear()
    assert machine._qemu_version("nope") == (0, 0)
    machine._qemu_version.cache_clear()


def test_native_available_requires_connector_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native needs BOTH the passt connector and qemu >= 10.1; either alone
    falls back to the sidecar."""
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: True)
    monkeypatch.setattr(machine, "_qemu_version", lambda _qb: (10, 1))
    assert machine._passt_native_available("q") is True
    # New enough qemu but no passt connector (e.g. macOS / jammy host).
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: False)
    assert machine._passt_native_available("q") is False
    # passt present but qemu too old (7.2 <= v < 10.1 -> sidecar only).
    monkeypatch.setattr(machine, "_passt_available", lambda _qb: True)
    monkeypatch.setattr(machine, "_qemu_version", lambda _qb: (10, 0))
    assert machine._passt_native_available("q") is False


def test_passt_native_auto_follows_version_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_PASST_NATIVE", raising=False)
    monkeypatch.setattr(machine, "_passt_native_available", lambda _qb: True)
    assert machine.resolve_passt_native("q") is True
    monkeypatch.setattr(machine, "_passt_native_available", lambda _qb: False)
    assert machine.resolve_passt_native("q") is False


def test_passt_native_off_pins_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollback knob: force the sidecar even where native is available."""
    monkeypatch.setenv("HOMELAB_PASST_NATIVE", "off")
    monkeypatch.setattr(machine, "_passt_native_available", lambda _qb: pytest.fail("probed despite off"))
    assert machine.resolve_passt_native("q") is False


def test_passt_native_on_errors_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_PASST_NATIVE", "on")
    monkeypatch.setattr(machine, "_passt_native_available", lambda _qb: False)
    monkeypatch.setattr(machine, "_qemu_version", lambda _qb: (9, 2))
    with pytest.raises(RuntimeError, match="native passt netdev is unavailable"):
        machine.resolve_passt_native("qemu-system-x86_64")


def test_passt_native_invalid_override_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_PASST_NATIVE", "bogus")
    with pytest.raises(RuntimeError, match="not in auto/on/off"):
        machine.resolve_passt_native("q")

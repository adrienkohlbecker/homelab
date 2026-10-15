"""Tests for the construction-time checks: the SSH key mode and the imagedir."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def test_qemu_preflight_normalizes_ssh_key_mode(
    tmp_path: Path,
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_key = tmp_path / "vagrant.key"
    ssh_key.write_text("test key")
    ssh_key.chmod(0o644)
    monkeypatch.setattr(machine, "SSH_KEY", str(ssh_key))

    machine_factory()

    assert ssh_key.stat().st_mode & 0o777 == 0o600


def test_qemu_imagedir_fails_without_its_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The imagedir is created on demand, but its parent volume never is."""
    monkeypatch.setenv("HOMELAB_CI_DIR", str(tmp_path / "unmounted" / "homelab_ci"))
    with pytest.raises(RuntimeError, match="does not exist"):
        machine.imagedir_for_host()


def test_qemu_imagedir_requires_mise_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_CI_DIR", raising=False)
    with pytest.raises(RuntimeError, match="through mise"):
        machine.imagedir_for_host()


def test_qemu_imagedir_uses_configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv("HOMELAB_CI_DIR", str(configured))

    assert machine.imagedir_for_host() == configured
    assert configured.is_dir()


# Captured at import, before conftest's autouse fixture stubs it out.
EXCLUDE_FROM_TIME_MACHINE = machine._exclude_from_time_machine


@pytest.mark.parametrize(
    ("system", "xattr_rc", "commands"),
    [("Darwin", 1, ["xattr", "tmutil"]), ("Darwin", 0, ["xattr"]), ("Linux", 1, [])],
)
def test_qemu_imagedir_is_excluded_from_time_machine_once_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, xattr_rc: int, commands: list[str]
) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, xattr_rc if cmd[0] == "xattr" else 0)

    imagedir = tmp_path / "homelab_ci"
    monkeypatch.setenv("HOMELAB_CI_DIR", str(imagedir))
    monkeypatch.setattr(machine.platform, "system", lambda: system)
    monkeypatch.setattr(machine.subprocess, "run", run)
    monkeypatch.setattr(machine, "_exclude_from_time_machine", EXCLUDE_FROM_TIME_MACHINE)

    machine.imagedir_for_host()

    assert [cmd[0] for cmd in calls] == commands
    if "tmutil" in commands:
        assert calls[1] == ["tmutil", "addexclusion", str(imagedir)]

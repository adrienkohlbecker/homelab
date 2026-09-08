"""Tests for the construction-time preflight checks.

Machine validates its required binaries during construction. Failures raise
RuntimeError with installation guidance. Linux image-root discovery also
rejects a missing /mnt/scratch/homelab_ci so the caller gets a clearer message
than tempfile's FileNotFoundError.
"""

from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def _which_excluding(missing: set[str]) -> Callable[[str], str | None]:
    """Return a shutil.which stub that pretends *missing* binaries aren't on PATH."""

    def _which(name: str) -> str | None:
        if name in missing:
            return None
        return f"/usr/local/bin/{name}"

    return _which


def test_qemu_preflight_raises_when_qemu_binary_missing(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"qemu-system-x86_64"}))
    with pytest.raises(RuntimeError, match="qemu-system-x86_64"):
        machine_factory(host_arch="x86_64")


def test_qemu_preflight_raises_when_timeout_missing(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"timeout"}))
    with pytest.raises(RuntimeError, match="'timeout' not found"):
        machine_factory()


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


def test_qemu_imagedir_missing_on_linux_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine on Linux fails fast when /mnt/scratch/homelab_ci isn't mounted.

    The Mac branch mkdirs packer/artifacts on the fly; the Linux branch
    hardcodes /mnt/scratch/homelab_ci and assumes the volume is mounted. Surface a
    clear error before tempfile fails later during Machine construction.
    """
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine.platform, "machine", lambda: "x86_64")
    monkeypatch.delenv("HOMELAB_CI_DIR", raising=False)
    monkeypatch.setattr(machine.Path, "is_dir", lambda self: False)
    with pytest.raises(RuntimeError, match="does not exist"):
        machine.Machine(
            machine="minimal",
            role="testrole",
            keep_vm=False,
            ubuntu_name="noble",
            machine_timeout=300,
        )


def test_qemu_imagedir_uses_configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOMELAB_CI_DIR", str(configured))

    assert machine.imagedir_for_host() == configured

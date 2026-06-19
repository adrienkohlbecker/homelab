"""Tests for the construction-time preflight checks.

Each Machine subclass now validates its required binaries via shutil.which
at the end of __post_init__. Failures raise RuntimeError with installation
guidance. Machine on Linux additionally rejects a missing /mnt/scratch/homelab_ci
so the caller gets a clearer message than tempfile's FileNotFoundError.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

import machine


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


def test_qemu_imagedir_missing_on_linux_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine on Linux fails fast when /mnt/scratch/homelab_ci isn't mounted.

    The Mac branch mkdirs packer/artifacts on the fly; the Linux branch
    hardcodes /mnt/scratch/homelab_ci and assumes the volume is mounted. Surface a
    clear error before tempfile blows up further down __post_init__.
    """
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(machine.Path, "is_dir", lambda self: False)
    with pytest.raises(RuntimeError, match="does not exist"):
        machine.Machine(
            machine="minimal",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )

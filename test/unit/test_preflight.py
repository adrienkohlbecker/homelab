"""Tests for the construction-time preflight checks.

Each Machine subclass now validates its required binaries via shutil.which
at the end of __post_init__. Failures raise RuntimeError with installation
guidance. The base class also rejects an imagedir that doesn't exist so the
caller gets a clearer message than tempfile's FileNotFoundError.
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
    qemu_machine_factory: Callable[..., machine.QemuMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"qemu-system-x86_64"}))
    with pytest.raises(RuntimeError, match="qemu-system-x86_64"):
        qemu_machine_factory(host_arch="x86_64")


def test_qemu_preflight_raises_when_timeout_missing(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"timeout"}))
    with pytest.raises(RuntimeError, match="'timeout' not found"):
        qemu_machine_factory()


def test_qemu_preflight_raises_when_lsof_missing(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"lsof"}))
    with pytest.raises(RuntimeError, match="'lsof' not found"):
        qemu_machine_factory()


def test_podman_preflight_raises_when_podman_missing(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(machine.shutil, "which", _which_excluding({"podman"}))
    with pytest.raises(RuntimeError, match="'podman' not found"):
        podman_machine_factory()


def test_imagedir_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing Machine against a non-existent imagedir raises a clear error.

    The Mac branch in subclass __init__ mkdir's packer/artifacts; the Linux
    branch just hardcodes /mnt/qemu. If that path isn't mounted on a fresh
    dev host, the user should hear about it before tempfile blows up.
    """
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    nonexistent = str(tmp_path / "does_not_exist")
    with pytest.raises(RuntimeError, match="does not exist or is not a directory"):
        machine.Machine(
            ssh_port=2222,
            ssh_user="vagrant",
            ansible_args=[],
            inventory_host="box",
            idfile="pid",
            imagedir=nonexistent,
            machine="box",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )

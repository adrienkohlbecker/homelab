"""Shared fixtures for the test/unit suite."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import machine


@pytest.fixture
def machine_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., machine.Machine]]:
    """Build Machine instances against a sandboxed OUT_DIR and imagedir.

    Each instance's TemporaryDirectory is cleaned up at fixture teardown so
    the dataclass's destructor warning doesn't fire.
    """
    out_dir = tmp_path / "out"
    monkeypatch.setattr(machine, "OUT_DIR", out_dir)
    image_dir = tmp_path / "images"
    image_dir.mkdir()

    instances: list[machine.Machine] = []

    def make(**overrides: Any) -> machine.Machine:
        defaults: dict[str, Any] = dict(
            ssh_port=2222,
            ssh_user="vagrant",
            ansible_args=["-e", '{"flag":true}'],
            inventory_host="box",
            idfile="pid",
            imagedir=str(image_dir),
            machine="box",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )
        defaults.update(overrides)
        m = machine.Machine(**defaults)
        instances.append(m)
        return m

    yield make

    for m in instances:
        m.workdir.cleanup()


def _sandbox_imagedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin imagedir resolution to tmp_path/packer/artifacts.

    Both QemuMachine and PodmanMachine compute imagedir from platform.system
    in __init__: Darwin -> Path("packer/artifacts").resolve() + mkdir;
    Linux -> /mnt/qemu (must already exist). Forcing the Darwin branch and
    chdir-ing to tmp_path keeps the tests host-agnostic and writable.
    """
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def qemu_machine_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., machine.QemuMachine]]:
    """Build QemuMachine instances with imagedir + arch under our control."""
    _sandbox_imagedir(tmp_path, monkeypatch)
    instances: list[machine.QemuMachine] = []

    def make(*, host_arch: str = "x86_64", **overrides: Any) -> machine.QemuMachine:
        # platform.machine is read by QemuMachine.host_arch on every access,
        # not at construction time, so the latest mock wins.
        monkeypatch.setattr(machine.platform, "machine", lambda: host_arch)
        defaults: dict[str, Any] = dict(
            machine="minimal",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )
        defaults.update(overrides)
        m = machine.QemuMachine(**defaults)
        instances.append(m)
        return m

    yield make
    for m in instances:
        m.workdir.cleanup()


@pytest.fixture
def podman_machine_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., machine.PodmanMachine]]:
    """Build PodmanMachine instances with imagedir under our control."""
    _sandbox_imagedir(tmp_path, monkeypatch)
    instances: list[machine.PodmanMachine] = []

    def make(**overrides: Any) -> machine.PodmanMachine:
        defaults: dict[str, Any] = dict(
            machine="container",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )
        defaults.update(overrides)
        m = machine.PodmanMachine(**defaults)
        instances.append(m)
        return m

    yield make
    for m in instances:
        m.workdir.cleanup()

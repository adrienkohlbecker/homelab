"""Shared fixtures for the unit_tests suite."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import machine


@pytest.fixture
def machine_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., machine.Machine]]:
    """Build base Machine instances against a sandboxed OUT_DIR.

    Each instance's TemporaryDirectory is cleaned up at fixture teardown so
    the dataclass's destructor warning doesn't fire.
    """
    out_dir = tmp_path / "out"
    monkeypatch.setattr(machine, "OUT_DIR", out_dir)

    instances: list[machine.Machine] = []

    def make(**overrides: Any) -> machine.Machine:
        defaults: dict[str, Any] = dict(
            ssh_port=2222,
            ssh_user="vagrant",
            ansible_args=["-e", '{"flag":true}'],
            inventory_host="box",
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


@pytest.fixture
def qemu_machine_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., machine.QemuMachine]]:
    """Build QemuMachine instances with imagedir + arch under our control."""
    # Pin host-platform discovery to Darwin so QemuMachine resolves
    # imagedir to tmp_path/packer/artifacts (writable, host-agnostic).
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    # These tests only build command lines -- they never spawn qemu -- so the
    # emulator binary needn't actually be installed. The x86 CI image ships
    # qemu-system-x86 but not the aarch64 emulator, so an unmocked which()
    # fails the aarch64 cases there (and the suite would otherwise silently
    # depend on whatever happens to be on PATH). Fake which() so preflight
    # passes for any arch; test_preflight overrides this per-test to exercise
    # the missing-binary path.
    monkeypatch.setattr(machine.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    monkeypatch.chdir(tmp_path)
    instances: list[machine.QemuMachine] = []

    def make(*, host_arch: str = "x86_64", **overrides: Any) -> machine.QemuMachine:
        # detect_host_arch() runs once inside QemuMachine.__init__ and the
        # ArchProfile gets cached on the instance, so the patch must be in
        # place before make() constructs the machine below.
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

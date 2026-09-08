"""Shared fixtures and helpers for the unit_tests suite."""

import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import machine
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_repo_module(relative_path: str, *, name: str | None = None) -> ModuleType:
    """Import a standalone program by repo-relative path.

    Registering the module before executing it lets dataclasses and other
    consumers of postponed annotations resolve it through the module registry.
    """

    module_path = _REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name or module_path.stem, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def machine_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., machine.Machine]]:
    """Build Machine instances with imagedir + arch under our control.

    Each instance's TemporaryDirectory is cleaned up at fixture teardown so
    the destructor warning doesn't fire.
    """
    # Pin host-platform discovery to Darwin so Machine resolves imagedir to
    # tmp_path/packer/artifacts (writable, host-agnostic).
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("HOMELAB_CI_DIR", raising=False)
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
    instances: list[machine.Machine] = []

    def make(
        *,
        host_arch: str = "x86_64",
        ansible_args: list[str] | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        **overrides: Any,
    ) -> machine.Machine:
        # detect_host_arch() runs once inside Machine.__init__ and the
        # ArchProfile gets cached on the instance, so the patch must be in
        # place before make() constructs the machine below.
        monkeypatch.setattr(machine.platform, "machine", lambda: host_arch)
        kwargs: dict[str, Any] = dict(
            machine="box",
            role="testrole",
            keep_vm=False,
            ubuntu_name="noble",
            machine_timeout=300,
        )
        kwargs.update(overrides)
        m = machine.Machine(**kwargs)
        if ansible_args is not None:
            m.ansible_args = ansible_args
        if ssh_port is not None:
            m.ssh_port = ssh_port
        if ssh_user is not None:
            m.ssh_user = ssh_user
        instances.append(m)
        return m

    yield make
    for m in instances:
        m.workdir.cleanup()

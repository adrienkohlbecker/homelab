"""Shared fixtures for the unit_tests suite."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import machine

_CONSTRUCTOR_PARAMS = frozenset(
    {
        "machine",
        "role",
        "keep_vm",
        "ubuntu_name",
        "machine_timeout",
        "upstream_mirrors",
        "workdir_parent",
        "launch",
    }
)


@pytest.fixture
def machine_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., machine.Machine]]:
    """Build Machine instances with imagedir + arch under our control.

    Each instance's TemporaryDirectory is cleaned up at fixture teardown so
    the destructor warning doesn't fire.
    """
    # Pin host-platform discovery to Darwin so Machine resolves imagedir to
    # tmp_path/packer/artifacts (writable, host-agnostic).
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
    instances: list[machine.Machine] = []

    def make(*, host_arch: str = "x86_64", **overrides: Any) -> machine.Machine:
        # detect_host_arch() runs once inside Machine.__init__ and the
        # ArchProfile gets cached on the instance, so the patch must be in
        # place before make() constructs the machine below.
        monkeypatch.setattr(machine.platform, "machine", lambda: host_arch)
        kwargs: dict[str, Any] = dict(
            machine="box",
            role="testrole",
            keep_vm=False,
            ubuntu_name="jammy",
            machine_timeout=300,
        )
        # Constructor params go to __init__; anything else is a synthetic
        # field value injected post-construction via setattr.
        post_init: dict[str, Any] = {}
        for key, value in overrides.items():
            if key in _CONSTRUCTOR_PARAMS:
                kwargs[key] = value
            else:
                post_init[key] = value
        m = machine.Machine(**kwargs)
        for key, value in post_init.items():
            setattr(m, key, value)
        instances.append(m)
        return m

    yield make
    for m in instances:
        m.workdir.cleanup()

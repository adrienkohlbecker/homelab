"""Shared fixtures and helpers for the unit_tests suite."""

import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import machine
import pytest
import utils

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
    # Pin the host platform to Darwin and the imagedir to a writable tmp path.
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOMELAB_CI_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(machine, "OUT_DIR", tmp_path / "out")
    monkeypatch.chdir(tmp_path)
    instances: list[machine.Machine] = []

    def make(
        *,
        host_arch: str = "x86_64",
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        **overrides: Any,
    ) -> machine.Machine:
        # Machine.__init__ resolves the host arch once, so the patch must be in
        # place before make() constructs the machine below.
        monkeypatch.setattr(machine.platform, "machine", lambda: host_arch)
        kwargs: dict[str, Any] = dict(
            machine="lab",
            role="testrole",
            keep_vm=False,
            ubuntu_name="noble",
            machine_timeout=300,
        )
        kwargs.update(overrides)
        m = machine.Machine(**kwargs)
        if ssh_port is not None:
            m.ssh_port = ssh_port
        if ssh_user is not None:
            m.ssh_user = ssh_user
        instances.append(m)
        return m

    yield make
    for m in instances:
        m.workdir.cleanup()


@pytest.fixture(autouse=True)
def _verbose_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test in the verbose console mode; testrole.main() switches the
    process-wide mode to compact."""
    monkeypatch.setattr(utils, "_CONSOLE_TAG", None)

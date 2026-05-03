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

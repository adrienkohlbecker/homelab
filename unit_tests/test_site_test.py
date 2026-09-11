"""Focused tests for the full-site harness modes."""

import asyncio
import contextlib
from pathlib import Path
from typing import cast

import pytest
import site_test
from machine import Machine


class CheckModeMachine:
    def __init__(self, workdir_path: Path) -> None:
        self.workdir_path = workdir_path
        self.keep_vm = False
        self.ansible_calls: list[tuple[str, ...]] = []
        self.ssh_calls: list[tuple[str, ...]] = []
        self.system_running_calls = 0

    async def __aenter__(self) -> CheckModeMachine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def session(self, timeout: int):
        return Machine.session(cast(Machine, self), timeout)

    async def ensure_booted(self) -> None:
        return None

    async def ensure_ssh(self) -> None:
        return None

    async def ensure_system_running(self) -> None:
        self.system_running_calls += 1

    async def ansible_command(self, *args: str, coverage_phase: str | None = None) -> None:
        self.ansible_calls.append((*args, f"phase={coverage_phase}"))

    async def ssh_command(self, *args: str, check: bool = True) -> None:
        self.ssh_calls.append(args)


def test_check_mode_forwards_flag_and_skips_poweroff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = CheckModeMachine(tmp_path)

    asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10, check_mode=True))

    assert machine.ansible_calls == [
        (str(tmp_path / "_environment.yml"), "phase=environment"),
        (
            str(tmp_path / "_site_check_prerequisites.yml"),
            "phase=site_check_prerequisites",
        ),
        (str(tmp_path / "site.yml"), "--check", "phase=site_check"),
    ]
    assert machine.system_running_calls == 1
    assert machine.ssh_calls == []

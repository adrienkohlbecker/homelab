"""Focused tests for the full-site harness modes."""

import asyncio
import contextlib
from pathlib import Path
from typing import cast

import pytest
import site_test
from utils import CommandResult


class CheckModeMachine:
    def __init__(self, workdir_path: Path) -> None:
        self.workdir_path = workdir_path
        self.keep_vm = False
        self.ansible_calls: list[tuple[str, ...]] = []
        self.ssh_calls: list[tuple[str, ...]] = []

    async def __aenter__(self) -> CheckModeMachine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def ensure_booted(self) -> None:
        return None

    async def ensure_ssh(self) -> None:
        return None

    async def ansible_command(self, *args: str) -> None:
        self.ansible_calls.append(args)

    async def ssh_command(self, *args: str, check: bool = True) -> CommandResult:
        self.ssh_calls.append(args)
        return CommandResult(0, ["running"], [])


def test_check_mode_forwards_flag_and_skips_poweroff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(site_test, "cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = CheckModeMachine(tmp_path)

    asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10, check_mode=True))

    assert machine.ansible_calls == [
        (str(tmp_path / "_environment.yml"),),
        (str(tmp_path / "site.yml"), "--check"),
    ]
    assert machine.ssh_calls == [("systemctl", "is-system-running", "--wait")]

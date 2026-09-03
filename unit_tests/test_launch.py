"""Focused tests for the standalone QEMU launcher."""

import asyncio
import contextlib
from typing import cast

import launch
import pytest
from utils import CommandResult


class LaunchMachine:
    def __init__(self) -> None:
        self.printed_ssh_instructions = False

    async def __aenter__(self) -> LaunchMachine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def ensure_booted(self) -> None:
        return None

    async def ensure_ssh(self) -> None:
        return None

    def print_ssh_instructions(self) -> None:
        self.printed_ssh_instructions = True

    async def ssh_command(self, *args: str, check: bool = True) -> CommandResult:
        return CommandResult(0, ["running"], [])


def test_exit_after_ready_skips_interactive_ssh_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = LaunchMachine()

    asyncio.run(
        launch._run_async(
            cast(launch.Machine, machine),
            wait_for_ssh=True,
            exit_after_ready=True,
            write_hostfwds=None,
        )
    )

    assert machine.printed_ssh_instructions is False

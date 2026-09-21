"""Focused tests for the standalone QEMU launcher."""

import asyncio
import contextlib
from typing import cast

import launch
import pytest


class LaunchMachine:
    def __init__(self) -> None:
        self.printed_ssh_instructions = False
        self.system_running_checked = False

    async def __aenter__(self) -> LaunchMachine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def ensure_booted(self) -> None:
        return None

    async def ensure_ssh(self) -> None:
        return None

    async def ensure_system_running(self) -> None:
        self.system_running_checked = True

    def print_ssh_instructions(self) -> None:
        self.printed_ssh_instructions = True


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
    assert machine.system_running_checked is True


def test_vcpus_flag_reaches_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_machine(**kwargs: object) -> object:
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(launch, "Machine", fake_machine)
    monkeypatch.setattr(launch.sys, "argv", ["launch.py", "--machine", "lab", "--vcpus", "1"])

    with pytest.raises(SystemExit):
        launch.main()

    assert captured["run_options"] == launch.MachineRunOptions(vcpus=1)

"""Tests for the shared machine lifecycle."""

import asyncio
import contextlib
from typing import cast

import machine as machine_module
import pytest
from machine import Machine


class FakeMachine:
    def __init__(self, *, keep_vm: bool = False) -> None:
        self.keep_vm = keep_vm
        self.entered = False
        self.exited = False
        self.waited = False
        self.instructions = False

    async def __aenter__(self) -> FakeMachine:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True

    def print_ssh_instructions(self) -> None:
        self.instructions = True

    async def wait(self) -> None:
        self.waited = True


def test_session_enters_and_exits_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine_module, "cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = FakeMachine()

    async def run() -> None:
        async with Machine.session(cast(Machine, machine), 10):
            assert machine.entered

    asyncio.run(run())

    assert machine.exited
    assert not machine.waited


def test_keep_waits_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine_module, "cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = FakeMachine(keep_vm=True)

    async def run() -> None:
        async with Machine.session(cast(Machine, machine), 10):
            pass

    asyncio.run(run())

    assert machine.instructions
    assert machine.waited


def test_keep_waits_after_timeout_then_resurfaces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine_module, "cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = FakeMachine(keep_vm=True)

    async def run() -> None:
        async with Machine.session(cast(Machine, machine), 0):
            await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    assert machine.instructions
    assert machine.waited
    assert machine.exited

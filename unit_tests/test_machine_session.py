"""Tests for the shared machine lifecycle."""

import time
from typing import cast

import pytest
from machine import Machine


class FakeMachine:
    def __init__(self, *, keep_vm: bool = False) -> None:
        self.keep_vm = keep_vm
        self.entered = False
        self.exited = False
        self.waited = False
        self.instructions = False

    def __enter__(self) -> FakeMachine:
        self.entered = True
        return self

    def __exit__(self, *args: object) -> None:
        self.exited = True

    def print_ssh_instructions(self) -> None:
        self.instructions = True

    def wait(self, timeout: float | None = None) -> None:
        self.waited = True


def test_session_enters_and_exits_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine()

    def run() -> None:
        with Machine.session(cast(Machine, machine), 10):
            assert machine.entered

    run()

    assert machine.exited
    assert not machine.waited


def test_keep_waits_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = FakeMachine(keep_vm=True)

    def run() -> None:
        with Machine.session(cast(Machine, machine), 10):
            pass

    run()

    assert machine.instructions
    assert machine.waited


def test_keep_waits_after_timeout_then_resurfaces_it() -> None:
    machine = FakeMachine(keep_vm=True)

    with pytest.raises(TimeoutError), Machine.session(cast(Machine, machine), 1):
        time.sleep(5)

    assert machine.instructions
    assert machine.waited
    assert machine.exited

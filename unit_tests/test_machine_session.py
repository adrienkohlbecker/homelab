"""Tests for the shared machine lifecycle."""

import os
import signal
import time
from typing import cast

import pytest
from machine import Machine


class FakeMachine:
    def __init__(self, *, keep_vm: bool = False, boot_error: BaseException | None = None) -> None:
        self.keep_vm = keep_vm
        self.boot_error = boot_error
        self.booted = False
        self.stopped = False
        self.waited = False
        self.instructions = False

    def prepare(self) -> None:
        pass

    def boot(self) -> None:
        if self.boot_error:
            raise self.boot_error
        self.booted = True

    def stop(self) -> None:
        self.stopped = True

    def print_ssh_instructions(self) -> None:
        self.instructions = True

    def wait(self, timeout: float | None = None) -> None:
        self.waited = True


def test_session_boots_and_stops_machine() -> None:
    machine = FakeMachine()

    with Machine.session(cast(Machine, machine), 10):
        assert machine.booted

    assert machine.stopped
    assert not machine.waited


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("qemu-img failed")])
def test_session_stops_machine_when_boot_is_interrupted(error: BaseException) -> None:
    machine = FakeMachine(boot_error=error)

    with pytest.raises(type(error)), Machine.session(cast(Machine, machine), 10):
        pytest.fail("the body must not run")

    assert machine.stopped


def test_keep_waits_after_success() -> None:
    machine = FakeMachine(keep_vm=True)

    with Machine.session(cast(Machine, machine), 10):
        pass

    assert machine.instructions
    assert machine.waited


def test_keep_waits_after_timeout_then_resurfaces_it() -> None:
    machine = FakeMachine(keep_vm=True)

    with pytest.raises(TimeoutError), Machine.session(cast(Machine, machine), 1):
        time.sleep(5)

    assert machine.instructions
    assert machine.waited
    assert machine.stopped


def test_ctrl_c_skips_the_keep_hold() -> None:
    machine = FakeMachine(keep_vm=True)

    with pytest.raises(KeyboardInterrupt), Machine.session(cast(Machine, machine), 10):
        raise KeyboardInterrupt

    assert not machine.waited
    assert machine.stopped


def test_sigterm_in_the_body_still_stops_the_machine() -> None:
    machine = FakeMachine(keep_vm=True)

    def terminated_body() -> None:
        with Machine.session(cast(Machine, machine), 10):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)

    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            terminated_body()
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert machine.stopped
    assert not machine.waited

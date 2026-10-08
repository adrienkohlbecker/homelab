"""Focused tests for the standalone QEMU launcher."""

import contextlib
from collections.abc import Iterator
from typing import cast

import launch
import pytest


class LaunchMachine:
    def __init__(self) -> None:
        self.session_timeout: int | None = 0
        self.system_running_checked = False

    @contextlib.contextmanager
    def session(self, timeout: int | None) -> Iterator[None]:
        self.session_timeout = timeout
        yield

    def ensure_booted(self) -> None:
        return None

    def ensure_ssh(self) -> None:
        return None

    def ensure_system_running(self) -> None:
        self.system_running_checked = True


@pytest.mark.parametrize("exit_after_ready", [True, False])
def test_only_exit_after_ready_checks_systemd_under_a_deadline(exit_after_ready: bool) -> None:
    machine = LaunchMachine()

    launch._run(cast(launch.Machine, machine), exit_after_ready=exit_after_ready)

    assert machine.system_running_checked is exit_after_ready
    assert machine.session_timeout == (launch.EXIT_AFTER_READY_TIMEOUT if exit_after_ready else None)


@pytest.mark.parametrize(
    ("argv", "keep_vm", "vcpus", "memory_mb"),
    [
        (["--vcpus", "1", "--mem", "2048"], True, 1, 2048),
        (["--exit-after-ready"], False, None, None),
    ],
)
def test_flags_reach_the_machine(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], keep_vm: bool, vcpus: int | None, memory_mb: int | None
) -> None:
    captured: dict[str, object] = {}

    def fake_machine(**kwargs: object) -> object:
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(launch, "Machine", fake_machine)
    monkeypatch.setattr(launch.sys, "argv", ["launch.py", "--machine", "lab", *argv])

    with pytest.raises(SystemExit):
        launch.main()

    assert captured["keep_vm"] is keep_vm
    assert (captured["vcpus"], captured["memory_mb"]) == (vcpus, memory_mb)

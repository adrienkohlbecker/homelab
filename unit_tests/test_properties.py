"""Tests for the small Machine / Machine properties.

Covers wrapper_timeout (Machine) and the cached ArchProfile on Machine.
"""

from collections.abc import Callable

import machine
import pytest


def test_wrapper_timeout_adds_grace_when_not_keeping(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    # The inner timeout/podman --timeout has to outlast the Python deadline;
    # WRAPPER_GRACE_SECONDS layers the cushion so callers don't have to.
    m = machine_factory(machine_timeout=600, keep_vm=False)
    assert m.wrapper_timeout == 600 + machine.Machine.WRAPPER_GRACE_SECONDS


def test_wrapper_timeout_is_zero_when_keeping(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    # `timeout 0` and `podman --timeout 0` both mean "no limit"; this is what
    # lets --keep sessions stay up indefinitely.
    m = machine_factory(machine_timeout=600, keep_vm=True)
    assert m.wrapper_timeout == 0


@pytest.mark.parametrize(
    ("platform_machine", "expected"),
    [
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("aarch64", "aarch64"),
        ("arm64", "aarch64"),
    ],
)
def test_arch_profile_normalises_known_machines(
    machine_factory: Callable[..., machine.Machine],
    platform_machine: str,
    expected: str,
) -> None:
    m = machine_factory(host_arch=platform_machine)
    assert m.arch.name == expected


def test_unknown_host_arch_fails_fast_at_construction(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    # detect_host_arch() runs once inside Machine.__init__, so an
    # unsupported platform raises before the instance exists.
    with pytest.raises(RuntimeError, match="Unsupported host architecture"):
        machine_factory(host_arch="riscv64")

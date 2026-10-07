"""Architecture data the harness boots from."""

from collections.abc import Callable
from unittest import mock

import machine
import pytest


def test_every_shared_architecture_has_keep_vm_devices() -> None:
    # Packer and the CI stores accept any architecture in the data file; the
    # harness must be able to boot each one.
    assert set(machine.ARCHITECTURES) == set(machine.KEEP_VM_DEVICES)


@pytest.mark.parametrize(
    ("platform_machine", "expected"),
    [("x86_64", "x86_64"), ("amd64", "x86_64"), ("aarch64", "aarch64"), ("arm64", "aarch64")],
)
def test_host_arch_normalizes_platform_machine(platform_machine: str, expected: str) -> None:
    with mock.patch.object(machine.platform, "machine", return_value=platform_machine):
        assert machine.host_arch() == expected


def test_host_arch_rejects_unknown() -> None:
    with (
        mock.patch.object(machine.platform, "machine", return_value="riscv64"),
        pytest.raises(RuntimeError, match="Unsupported"),
    ):
        machine.host_arch()


def test_uefi_needs_the_host_os_pair(machine_factory: Callable[..., machine.Machine]) -> None:
    m = machine_factory(host_arch="x86_64")  # the fixture pins a Darwin host
    with pytest.raises(RuntimeError, match="No x86_64 UEFI firmware is defined for darwin hosts"):
        m._uefi_drives()

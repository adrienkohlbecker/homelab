"""Sanity check that pytest collects this directory and the harness imports."""

import machine


def test_machine_module_exposes_public_api() -> None:
    assert hasattr(machine, "Machine")
    assert hasattr(machine, "QEMU_MACHINE_SPECS")

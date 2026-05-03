"""Tests for QemuMachine._boot_command across the arch/keep_vm/direct-boot matrix.

prepare() does the IO-heavy work of populating drives / _direct_boot /
_extra_disk_devices, which aren't safe to run in a unit test (qemu-img,
extraction VM). Each test sets those attrs manually and asserts on the
shape of the assembled command line. Locks in the invariants that the
upcoming ArchProfile / memory_mb / -name refactors will touch.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

import machine


def _setup(m: machine.QemuMachine, drives: list[str] | None = None) -> None:
    """Bypass prepare(): give the instance the attributes _boot_command reads."""
    m.drives = list(drives or [])
    m._direct_boot = None
    m._extra_disk_devices = []


def test_default_x86_64_no_keep_no_direct_boot(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="x86_64", keep_vm=False, machine_timeout=600)
    _setup(m, drives=["file=disk1.qcow2,if=virtio", "file=disk2.qcow2,if=virtio"])
    cmd = m._boot_command()

    # GNU timeout wrapper -- the 10s kill-after gives the qemu signal handler
    # a window before SIGKILL.
    assert cmd[0] == "timeout"
    assert cmd[1] == "--kill-after=10s"
    assert cmd[2] == "600"
    assert cmd[3] == "qemu-system-x86_64"

    # Drives expand to repeated --drive args.
    assert cmd.count("--drive") == 2
    drive_idx = [i for i, a in enumerate(cmd) if a == "--drive"]
    assert cmd[drive_idx[0] + 1] == "file=disk1.qcow2,if=virtio"
    assert cmd[drive_idx[1] + 1] == "file=disk2.qcow2,if=virtio"

    # Machine type / accel: x86_64 -> q35; Darwin (forced by fixture) -> hvf.
    machine_idx = cmd.index("-machine")
    assert cmd[machine_idx + 1] == "type=q35,accel=hvf"

    # Hardcoded sizing (changes when memory_mb / vcpus get plumbed through).
    assert cmd[cmd.index("-smp") + 1] == "8,sockets=8"
    assert cmd[cmd.index("-m") + 1] == "4096M"
    assert cmd[cmd.index("-cpu") + 1] == "host"
    assert cmd[cmd.index("-name") + 1] == "packer-ubuntu"

    # Headless when not keeping the VM.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == "none"

    # No direct -kernel boot in this configuration.
    assert "-kernel" not in cmd
    assert "-initrd" not in cmd
    assert "-append" not in cmd

    # Pidfile under the workdir.
    assert cmd[cmd.index("-pidfile") + 1] == f"{m.workdir.name}/pid"

    # Serial console plumbed to stdio so kernel printk lands in the boot log.
    assert cmd[cmd.index("-serial") + 1] == "stdio"

    # SSH forward listens on a kernel-picked port (the 0 in 0-:22).
    netdev_idx = cmd.index("-netdev")
    assert cmd[netdev_idx + 1] == f"user,id=user.0,hostfwd=tcp:{m.ssh_host}:0-:22"


def test_default_aarch64_no_keep_no_direct_boot(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="aarch64")
    _setup(m)
    cmd = m._boot_command()

    assert cmd[3] == "qemu-system-aarch64"
    assert cmd[cmd.index("-machine") + 1] == "type=virt,accel=hvf"


def test_keep_vm_zero_timeout_x86_64_uses_minimal_keep_devices(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="x86_64", keep_vm=True, machine_timeout=600)
    _setup(m)
    cmd = m._boot_command()

    # keep_vm collapses the wrapper timeout to 0 ("no timeout" in GNU timeout).
    assert cmd[2] == "0"

    # x86_64 q35 has VGA / PS/2 / ICH9 USB by default; only usb-tablet is
    # added (absolute mouse for VNC). No virtio-gpu-pci.
    assert "virtio-gpu-pci" not in cmd

    # VNC display + French keyboard layout.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == "vnc=:0,to=99"
    assert cmd[cmd.index("-k") + 1] == "fr"

    # usb-tablet is the only -device addition for keep_vm on x86_64.
    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    assert "usb-tablet" in devices


def test_keep_vm_aarch64_adds_full_input_stack(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="aarch64", keep_vm=True)
    _setup(m)
    cmd = m._boot_command()

    # virt has no default graphics or input -- needs the full set.
    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    for needed in ("virtio-gpu-pci", "qemu-xhci", "usb-kbd", "usb-tablet"):
        assert needed in devices


def test_direct_boot_aarch64_appends_console_when_missing(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="aarch64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs=rpool/ROOT/ubuntu_xyz",
    )
    cmd = m._boot_command()

    assert cmd[cmd.index("-kernel") + 1] == "/cache/kernel"
    assert cmd[cmd.index("-initrd") + 1] == "/cache/initrd"

    append = cmd[cmd.index("-append") + 1]
    # Original cmdline preserved verbatim, followed by the arch-specific UART.
    assert append.startswith("root=zfs=rpool/ROOT/ubuntu_xyz")
    assert "console=ttyAMA0,115200" in append
    assert "earlycon=pl011,0x9000000" in append
    # No tty0 added without keep_vm (no graphics device to bind fbcon to).
    assert "console=tty0" not in append


def test_direct_boot_aarch64_does_not_duplicate_existing_ttyAMA(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="aarch64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs=rpool/ROOT/ubuntu_xyz console=ttyAMA0 quiet",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    # Only one console=ttyAMA in the final cmdline -- the user-provided one.
    assert append.count("console=ttyAMA") == 1


def test_direct_boot_x86_64_appends_ttyS(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="x86_64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs=rpool/ROOT/ubuntu_xyz",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    assert "console=ttyS0,115200" in append
    assert "earlycon=uart8250,io,0x3f8" in append


def test_direct_boot_keep_vm_inserts_tty0_first(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    m = qemu_machine_factory(host_arch="aarch64", keep_vm=True)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs=rpool/ROOT/ubuntu_xyz",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    # tty0 must appear before the serial console=, because Linux makes the
    # LAST console= the primary /dev/console (we want serial primary).
    tty0_idx = append.index("console=tty0")
    serial_idx = append.index("console=ttyAMA")
    assert tty0_idx < serial_idx


@pytest.mark.parametrize("host_arch", ["x86_64", "aarch64"])
def test_pidfile_uses_idfile_under_workdir(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
    host_arch: str,
) -> None:
    m = qemu_machine_factory(host_arch=host_arch)
    _setup(m)
    cmd = m._boot_command()
    assert cmd[cmd.index("-pidfile") + 1] == f"{m.workdir.name}/{m.idfile}"

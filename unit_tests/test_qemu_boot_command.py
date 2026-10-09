"""Tests for Machine._boot_command across the arch/keep_vm/direct-boot matrix.

prepare() does the IO-heavy work of populating drives, which isn't safe to run
in a unit test (qemu-img, file IO against Packer artifacts). Each test supplies
the remaining state directly and asserts on the assembled command line.
"""

from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def _setup(m: machine.Machine, drives: list[str] | None = None) -> None:
    """Bypass prepare(): give the instance the attributes _boot_command reads."""
    m.drives = list(drives or [])


def test_default_x86_64_no_keep_no_direct_boot(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", machine="minimal", keep_vm=False, machine_timeout=600)
    _setup(m, drives=["file=disk1.qcow2,if=virtio", "file=disk2.qcow2,if=virtio"])
    cmd = m._boot_command()

    # GNU timeout wrapper -- the 10s kill-after gives the qemu signal handler
    # a window before SIGKILL. The wrapper outlasts machine_timeout by 60s, so
    # the session deadline fires first.
    assert cmd[0] == "timeout"
    assert cmd[1] == "--kill-after=10s"
    assert cmd[2] == "660"
    assert cmd[3] == "qemu-system-x86_64"

    # No implicit devices; the display comes back with a VGA BIOS for SeaBIOS.
    assert cmd[4] == "-nodefaults"
    assert "virtio-vga" in [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]

    # Drives expand to repeated --drive args.
    assert cmd.count("--drive") == 2
    drive_idx = [i for i, a in enumerate(cmd) if a == "--drive"]
    assert cmd[drive_idx[0] + 1] == "file=disk1.qcow2,if=virtio"
    assert cmd[drive_idx[1] + 1] == "file=disk2.qcow2,if=virtio"

    # Machine type / accel: x86_64 -> q35; Darwin (forced by fixture) -> hvf.
    machine_idx = cmd.index("-machine")
    assert cmd[machine_idx + 1] == "type=q35,accel=hvf,usb=on"

    # Sizing flows from QemuMachineSpec; minimal is sized down.
    assert cmd[cmd.index("-smp") + 1] == "2,sockets=1,cores=2"
    assert cmd[cmd.index("-m") + 1] == "2048M"

    # Headless when not keeping the VM.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == "none"

    # No firmware front-page countdown before the boot entry runs.
    assert cmd[cmd.index("-boot") + 1] == "menu=on,splash-time=0"

    # No direct -kernel boot in this configuration.
    assert "-kernel" not in cmd
    assert "-append" not in cmd

    # Pidfile under the workdir.
    assert cmd[cmd.index("-pidfile") + 1] == str(m.pid_file)

    # Serial console plumbed to stdio so kernel printk lands in the boot log.
    assert cmd[cmd.index("-serial") + 1] == "stdio"

    # Every hostfwd asks qemu for a free loopback port (0).
    netdev = cmd[cmd.index("-netdev") + 1].split(",")
    assert netdev[:3] == ["user", "id=user.0", f"hostfwd=tcp:{machine.SSH_HOST}:0-:22"]
    assert f"hostfwd=udp:{machine.SSH_HOST}:0-:51820" in netdev

    # QMP is how the harness learns those ports.
    assert cmd[cmd.index("-qmp") + 1] == f"unix:{m.qmp_socket},server=on,wait=off"


def test_macos_aarch64_uses_hvf(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64")
    _setup(m)
    cmd = m._boot_command()

    assert cmd[3] == "qemu-system-aarch64"
    assert cmd[cmd.index("-machine") + 1] == "type=virt,accel=hvf,usb=on"

    # virt has no legacy VGA; headless cells get no keep-VM input devices.
    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    assert "virtio-gpu-pci" in devices
    assert "usb-kbd" not in devices


def test_linux_aarch64_uses_kvm(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = machine_factory(host_arch="aarch64")
    _setup(m)
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")

    cmd = m._boot_command()

    assert cmd[3] == "qemu-system-aarch64"
    assert cmd[cmd.index("-machine") + 1] == "type=virt,accel=kvm,usb=on"
    assert "virtio-net,romfile=,netdev=user.0" in cmd


def test_keep_vm_zero_timeout_x86_64_uses_minimal_keep_devices(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", keep_vm=True, machine_timeout=600)
    _setup(m)
    cmd = m._boot_command()

    # A kept VM runs unwrapped, until the operator stops it.
    assert cmd[0] == "qemu-system-x86_64"

    # x86_64 q35 has PS/2 / ICH9 USB by default; only usb-tablet is added
    # (absolute mouse for VNC).
    # VNC on the first free loopback display + French keyboard layout.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == f"vnc={machine.SSH_HOST}:0,to=99"
    assert cmd[cmd.index("-k") + 1] == "fr"

    # usb-tablet is the only -device addition for keep_vm on x86_64.
    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    assert "usb-tablet" in devices


def test_keep_vm_display_window_uses_local_qemu_backend(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", keep_vm=True, launch=machine.LaunchOptions(display_window=True))
    _setup(m)
    cmd = m._boot_command()

    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == "cocoa"
    assert not any(a.startswith("vnc=") for a in cmd)


def test_keep_vm_aarch64_adds_full_input_stack(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64", keep_vm=True)
    _setup(m)
    cmd = m._boot_command()

    # virt has no default graphics or input -- needs the full set.
    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    for needed in ("virtio-gpu-pci", "qemu-xhci", "usb-kbd", "usb-tablet"):
        assert needed in devices


def test_direct_boot_passes_kernel_and_cmdline_verbatim(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(
        host_arch="aarch64",
        keep_vm=True,
        launch=machine.LaunchOptions(kernel=Path("/cache/zfsbootmenu.EFI"), append="zbm.show console=ttyAMA0"),
    )
    _setup(m)
    cmd = m._boot_command()

    assert cmd[cmd.index("-kernel") + 1] == "/cache/zfsbootmenu.EFI"
    assert cmd[cmd.index("-append") + 1] == "zbm.show console=ttyAMA0"


def test_resource_arguments_override_the_machine_spec(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", vcpus=2, memory_mb=12345)
    _setup(m)
    cmd = m._boot_command()
    assert cmd[cmd.index("-m") + 1] == "12345M"
    # -smp emits a single-socket layout with one core per vcpu.
    assert cmd[cmd.index("-smp") + 1] == "2,sockets=1,cores=2"

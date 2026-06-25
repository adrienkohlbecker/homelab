"""Tests for Machine._boot_command across the arch/keep_vm/direct-boot matrix.

prepare() does the IO-heavy work of populating drives / _direct_boot /
_extra_disk_devices, which aren't safe to run in a unit test (qemu-img,
file IO against packer artifacts). Each test sets those attrs manually
and asserts on the shape of the assembled command line.
"""

from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def test_wan_probe_ports_manifest_loads_shared_surface() -> None:
    assert machine.DEFAULT_WAN_FORWARDS == {
        "tcp": (32400, 51413),
        "udp": (51820, 51413, 5353, 41641, 41642),
    }


def _setup(m: machine.Machine, drives: list[str] | None = None) -> None:
    """Bypass prepare(): give the instance the attributes _boot_command reads."""
    m.drives = list(drives or [])
    m._direct_boot = None
    m._extra_disk_devices = []
    # prepare() picks vnc_display when keep_vm; bypass tests pin it so the
    # cmdline has a deterministic value.
    m.vnc_display = 0


def test_default_x86_64_no_keep_no_direct_boot(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", machine="minimal", keep_vm=False, machine_timeout=600)
    _setup(m, drives=["file=disk1.qcow2,if=virtio", "file=disk2.qcow2,if=virtio"])
    cmd = m._boot_command()

    # GNU timeout wrapper -- the 10s kill-after gives the qemu signal handler
    # a window before SIGKILL. wrapper_timeout = machine_timeout +
    # WRAPPER_GRACE_SECONDS (60s); the wrapper has to outlast the inner
    # asyncio.timeout in run_test.
    assert cmd[0] == "timeout"
    assert cmd[1] == "--kill-after=10s"
    assert cmd[2] == str(600 + machine.Machine.WRAPPER_GRACE_SECONDS)
    assert cmd[3] == "qemu-system-x86_64"

    # Drives expand to repeated --drive args.
    assert cmd.count("--drive") == 2
    drive_idx = [i for i, a in enumerate(cmd) if a == "--drive"]
    assert cmd[drive_idx[0] + 1] == "file=disk1.qcow2,if=virtio"
    assert cmd[drive_idx[1] + 1] == "file=disk2.qcow2,if=virtio"

    # Machine type / accel: x86_64 -> q35; Darwin (forced by fixture) -> hvf.
    machine_idx = cmd.index("-machine")
    assert cmd[machine_idx + 1] == "type=q35,accel=hvf,usb=on"

    # Sizing flows from QemuMachineSpec; "minimal" is sized down to
    # 2048M / 2 vcpus.
    assert cmd[cmd.index("-smp") + 1] == "2,sockets=1,cores=2"
    assert cmd[cmd.index("-m") + 1] == "2048M"
    assert cmd[cmd.index("-cpu") + 1] == "host"
    # -name distinguishes parallel runs in ps/pgrep output.
    assert cmd[cmd.index("-name") + 1] == f"homelab-{m.machine}-{m.role}"

    # Headless when not keeping the VM.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == "none"

    # No direct -kernel boot in this configuration.
    assert "-kernel" not in cmd
    assert "-initrd" not in cmd
    assert "-append" not in cmd

    # Pidfile under the workdir.
    assert cmd[cmd.index("-pidfile") + 1] == str(m.pid_file)

    # Serial console plumbed to stdio so kernel printk lands in the boot log.
    assert cmd[cmd.index("-serial") + 1] == "stdio"

    # _setup() bypasses prepare(), so only the always-present SSH hostfwd
    # appears until prepare() populates the controller-side WAN forwards.
    netdev_idx = cmd.index("-netdev")
    assert cmd[netdev_idx + 1] == (f"user,id=user.0," f"hostfwd=tcp:{machine.SSH_HOST}:{m.ssh_port}-:22")


def test_default_aarch64_no_keep_no_direct_boot(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64")
    _setup(m)
    cmd = m._boot_command()

    assert cmd[3] == "qemu-system-aarch64"
    assert cmd[cmd.index("-machine") + 1] == "type=virt,accel=hvf,usb=on"


def test_keep_vm_zero_timeout_x86_64_uses_minimal_keep_devices(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", keep_vm=True, machine_timeout=600)
    _setup(m)
    cmd = m._boot_command()

    # keep_vm collapses the wrapper timeout to 0 ("no timeout" in GNU timeout).
    assert cmd[2] == "0"

    # x86_64 q35 has VGA / PS/2 / ICH9 USB by default; only usb-tablet is
    # added (absolute mouse for VNC). No virtio-gpu-pci.
    assert "virtio-gpu-pci" not in cmd

    # VNC display + French keyboard layout. _setup() pinned vnc_display=0
    # so the cmdline is deterministic. VNC binds the cell's loopback (not the
    # wildcard host) so the reservation and the bind agree.
    display_idx = cmd.index("-display")
    assert cmd[display_idx + 1] == f"vnc={machine.SSH_HOST}:0"
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


def test_direct_boot_aarch64_appends_console_when_missing(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs:rpool/ROOT/ubuntu_xyz",
    )
    cmd = m._boot_command()

    assert cmd[cmd.index("-kernel") + 1] == "/cache/kernel"
    assert cmd[cmd.index("-initrd") + 1] == "/cache/initrd"

    append = cmd[cmd.index("-append") + 1]
    # Original cmdline preserved verbatim, followed by the arch-specific UART.
    assert append.startswith("root=zfs:rpool/ROOT/ubuntu_xyz")
    assert "console=ttyAMA0,115200" in append
    assert "earlycon=pl011,0x9000000" in append
    # No tty0 added without keep_vm (no graphics device to bind fbcon to).
    assert "console=tty0" not in append


def test_direct_boot_aarch64_does_not_duplicate_existing_ttyAMA(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs:rpool/ROOT/ubuntu_xyz console=ttyAMA0 quiet",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    # Only one console=ttyAMA in the final cmdline -- the user-provided one.
    assert append.count("console=ttyAMA") == 1


def test_direct_boot_x86_64_appends_ttyS(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="x86_64", keep_vm=False)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs:rpool/ROOT/ubuntu_xyz",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    assert "console=ttyS0,115200" in append
    assert "earlycon=uart8250,io,0x3f8" in append


def test_direct_boot_keep_vm_inserts_tty0_first(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(host_arch="aarch64", keep_vm=True)
    _setup(m)
    m._direct_boot = (
        Path("/cache/kernel"),
        Path("/cache/initrd"),
        "root=zfs:rpool/ROOT/ubuntu_xyz",
    )
    append = m._boot_command()[m._boot_command().index("-append") + 1]
    # tty0 must appear before the serial console=, because Linux makes the
    # LAST console= the primary /dev/console (we want serial primary).
    tty0_idx = append.index("console=tty0")
    serial_idx = append.index("console=ttyAMA")
    assert tty0_idx < serial_idx


def test_memory_and_vcpus_flow_from_spec(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    """Mutating the spec's memory_mb/vcpus fields must reach the qemu cmdline."""
    m = machine_factory(host_arch="x86_64")
    _setup(m)
    m._spec = m._spec._replace(memory_mb=12345, vcpus=2)
    cmd = m._boot_command()
    assert cmd[cmd.index("-m") + 1] == "12345M"
    # -smp emits a single-socket layout with one core per vcpu.
    assert cmd[cmd.index("-smp") + 1] == "2,sockets=1,cores=2"


@pytest.mark.parametrize("host_arch", ["x86_64", "aarch64"])
def test_pidfile_lives_under_workdir(
    machine_factory: Callable[..., machine.Machine],
    host_arch: str,
) -> None:
    m = machine_factory(host_arch=host_arch)
    _setup(m)
    cmd = m._boot_command()
    assert cmd[cmd.index("-pidfile") + 1] == str(m.pid_file)


def test_passt_backend_uses_stream_netdev(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    """On the passt backend qemu attaches via a stream netdev to the sidecar
    socket, with no slirp user-net or hostfwds in the cmdline."""
    # The Darwin fixture resolves slirp; force passt + the socket prepare()
    # would set (which _setup bypasses) to exercise the passt branch.
    m = machine_factory(host_arch="x86_64", machine="box")
    _setup(m, drives=["file=disk1.raw,if=virtio"])
    m._net_backend = "passt"
    m._passt_socket = m.workdir_path / "passt.sock"
    cmd = m._boot_command()

    netdev = cmd[cmd.index("-netdev") + 1]
    assert netdev == f"stream,id=net0,server=off,addr.type=unix,addr.path={m._passt_socket}"
    assert "hostfwd" not in netdev
    assert "user,id=user.0" not in netdev

    devices = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]
    assert "virtio-net,netdev=net0" in devices
    assert "virtio-net,netdev=user.0" not in devices


def test_passt_command_forwards_mirror_slirp_ports(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    """passt's --tcp-ports/--udp-ports forward the same three controller-side
    ports slirp hostfwds, 127.0.0.1-bound, and pin the topology address."""
    m = machine_factory(host_arch="x86_64", machine="box")
    _setup(m)
    m._net_backend = "passt"
    m._passt_socket = m.workdir_path / "passt.sock"
    m.ssh_port = 2222
    m.wan_forward_ports = {
        "tcp": {"32400": 3000},
        "udp": {"51820": 4000},
    }
    cmd = m._passt_command()

    assert cmd[0] == "passt"
    assert "--one-off" in cmd  # self-reap when qemu disconnects
    assert cmd[cmd.index("--socket") + 1] == str(m._passt_socket)
    # Single addr/ prefix binds the whole list (repeating it is rejected).
    assert cmd[cmd.index("--tcp-ports") + 1] == f"{machine.SSH_HOST}/2222:22,3000:32400"
    assert cmd[cmd.index("--udp-ports") + 1] == f"{machine.SSH_HOST}/4000:51820"
    # box is in the topology (10.123 -> 10.234 test view) -> address pinned.
    assert cmd[cmd.index("--address") + 1].startswith("10.234.")
    assert cmd[cmd.index("--gateway") + 1].startswith("10.234.")


def test_passt_command_skips_address_pin_off_topology(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    """minimal isn't in the topology, so passt assigns from the container's
    default-route interface -- no --address pin (mirrors slirp's default net)."""
    m = machine_factory(host_arch="x86_64", machine="minimal")
    _setup(m)
    m._net_backend = "passt"
    m._passt_socket = m.workdir_path / "passt.sock"
    cmd = m._passt_command()
    assert "--address" not in cmd
    assert "--gateway" not in cmd

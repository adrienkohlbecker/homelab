"""Per-architecture data driving the QEMU test harness.

Replaces the scattered `if self.host_arch == "x86_64": ... else ...` checks
in machine.py with a single frozen dataclass per supported host arch and a
detect_host_arch() factory. Adding a new arch is a matter of adding one
profile constant and an entry in the platform.machine() lookup; no other
call site has to grow another conditional.
"""

from __future__ import annotations

import dataclasses
import platform
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class ArchProfile:
    """Everything the test harness needs to know about a host arch.

    All fields are pure data; behaviour stays in the call sites that consume
    them. Frozen so an instance can be safely shared across QemuMachines.
    """

    name: str
    qemu_binary: str
    machine_type: str
    cloud_image_suffix: str
    # Substring used to detect whether a user-supplied kernel cmdline
    # already configures this arch's serial UART -- if found, we don't
    # append a duplicate console=/earlycon= line.
    serial_console_token: str
    # The full "console=<device>,<baud> earlycon=<...>" string we append
    # when the cmdline doesn't already wire up the UART.
    serial_console_default: str
    # Extra -device flags qemu needs in interactive (VNC) mode. q35 brings
    # std VGA / PS/2 / ICH9 USB by default, so x86_64 only needs usb-tablet
    # for absolute-coordinate mouse; aarch64 virt has no default graphics
    # or input and needs the full virtio-gpu + xhci + kbd + tablet set.
    keep_vm_extra_devices: tuple[str, ...]
    # Ordered candidate paths for the EDK2/OVMF CODE blob. First existing
    # path wins. Covers Homebrew on macOS plus the canonical Linux distro
    # locations.
    uefi_code_candidates: tuple[str, ...]
    # x86_64's q35 falls back to SeaBIOS off the OS disk, so the cloud-image
    # minimal variant doesn't need UEFI pflash. aarch64 virt only boots via
    # UEFI -- pflash must be attached even on minimal.
    bios_boot_supported: bool
    # On aarch64 the rEFInd -> ZFSBootMenu -> kexec chain in the packer
    # qcow2 panics on EDK2 (see notes/zbm-aarch64-kexec-bug-report.md), so
    # ZFS variants direct-boot the on-pool kernel/initrd. x86_64 can boot
    # the firmware chain normally.
    direct_boot_required_for_zfs: bool


X86_64 = ArchProfile(
    name="x86_64",
    qemu_binary="qemu-system-x86_64",
    machine_type="q35",
    cloud_image_suffix="amd64",
    serial_console_token="console=ttyS",
    serial_console_default="console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200",
    keep_vm_extra_devices=("-device", "usb-tablet"),
    uefi_code_candidates=(
        # Homebrew QEMU on macOS:
        "/opt/homebrew/share/qemu/edk2-x86_64-code.fd",
        "/usr/local/share/qemu/edk2-x86_64-code.fd",
        # Debian/Ubuntu (ovmf package):
        "/usr/share/OVMF/OVMF_CODE.fd",
        # Fedora/RHEL (edk2-ovmf package):
        "/usr/share/edk2/ovmf/OVMF_CODE.fd",
        "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd",
    ),
    bios_boot_supported=True,
    direct_boot_required_for_zfs=False,
)


AARCH64 = ArchProfile(
    name="aarch64",
    qemu_binary="qemu-system-aarch64",
    machine_type="virt",
    cloud_image_suffix="arm64",
    serial_console_token="console=ttyAMA",
    serial_console_default="console=ttyAMA0,115200 earlycon=pl011,0x9000000,115200",
    keep_vm_extra_devices=(
        "-device",
        "virtio-gpu-pci",
        "-device",
        "qemu-xhci",
        "-device",
        "usb-kbd",
        "-device",
        "usb-tablet",
    ),
    uefi_code_candidates=(
        # Homebrew QEMU on macOS:
        "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
        "/usr/local/share/qemu/edk2-aarch64-code.fd",
        # Debian/Ubuntu (qemu-efi-aarch64 package):
        "/usr/share/AAVMF/AAVMF_CODE.fd",
        "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    ),
    bios_boot_supported=False,
    direct_boot_required_for_zfs=True,
)


_BY_PLATFORM_MACHINE: dict[str, ArchProfile] = {
    "x86_64": X86_64,
    "amd64": X86_64,
    "aarch64": AARCH64,
    "arm64": AARCH64,
}


def detect_host_arch() -> ArchProfile:
    """Return the ArchProfile matching the current host's platform.machine()."""
    m = platform.machine()
    profile = _BY_PLATFORM_MACHINE.get(m)
    if profile is None:
        raise RuntimeError(f"Unsupported host architecture: {m}")
    return profile


def profile_for_name(name: str) -> ArchProfile:
    """Look up an ArchProfile by its canonical .name field ("x86_64"/"aarch64")."""
    if name == X86_64.name:
        return X86_64
    if name == AARCH64.name:
        return AARCH64
    raise RuntimeError(f"Unknown arch profile: {name}")


def uefi_code_path_for(profile: ArchProfile) -> Path:
    """Locate the EDK2/OVMF CODE blob matching *profile* on this host.

    Searches uefi_code_candidates in order; first existing path wins.
    Raises RuntimeError with installation guidance if none are present.
    """
    for c in profile.uefi_code_candidates:
        if Path(c).exists():
            return Path(c)
    raise RuntimeError(
        f"No {profile.name} UEFI firmware found in {list(profile.uefi_code_candidates)}. "
        "Install via `brew install qemu` (macOS), "
        "`apt install ovmf` / `apt install qemu-efi-aarch64` (Debian/Ubuntu), or "
        "`dnf install edk2-ovmf` (Fedora/RHEL)."
    )

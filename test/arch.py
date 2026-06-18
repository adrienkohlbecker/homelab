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

# Newer edk2 firmware fetched by `mise run test:firmware` into a gitignored
# path (symlinked across worktrees by mise-tasks/worktree/populate.sh, so one
# fetch in the main checkout covers all). Homebrew's qemu (through 11.0.1)
# bundles edk2-stable202408, whose DXE pool allocator hits a heap ASSERT in
# MdeModulePkg/Core/Dxe/Mem/Pool.c when rEFInd boots the OS across an aarch64
# *warm* reboot (`systemctl reboot`) -- the cold first boot is fine, so it only
# bites tests that reboot (hwe_kernel seed, reboot/kdump/console _verify).
# edk2-stable202511 fixes it. Required on aarch64 (set as required_firmware
# below): uefi_code_path_for raises with fetch guidance when it is absent rather
# than silently falling back to Homebrew's broken blob. macOS-only concern:
# aarch64 qemu is the local fixture; CI runs x86 EC2 cells and prod is amd64.
_AARCH64_PINNED_FIRMWARE = Path(__file__).resolve().parent / "firmware" / "edk2-aarch64-code-202511.fd"


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
    # A firmware blob the harness fetches itself and *requires* over any
    # system-provided one. When set, uefi_code_path_for returns it (or raises
    # with fetch guidance if absent) and never consults uefi_code_candidates.
    # None = use the candidate search. aarch64 pins a newer edk2 (see above).
    required_firmware: Path | None = None


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
        # Debian/Ubuntu (ovmf package). Ubuntu 24.04 dropped the legacy
        # non-4M OVMF_CODE.fd in favour of the 4M variant; older releases
        # still ship the legacy name. Try both.
        "/usr/share/OVMF/OVMF_CODE_4M.fd",
        "/usr/share/OVMF/OVMF_CODE.fd",
        # Fedora/RHEL (edk2-ovmf package):
        "/usr/share/edk2/ovmf/OVMF_CODE.fd",
        "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd",
    ),
    bios_boot_supported=True,
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
    # aarch64 requires the fetched edk2 (required_firmware below); the candidate
    # search is unused because Homebrew's/the distro's bundled blob ASSERTs on
    # warm reboot (see above).
    uefi_code_candidates=(),
    bios_boot_supported=False,
    required_firmware=_AARCH64_PINNED_FIRMWARE,
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

    When the profile pins a required_firmware, return it (or raise with fetch
    guidance if absent) -- the harness-managed blob is mandatory and we never
    fall back to a system one. Otherwise search uefi_code_candidates in order;
    first existing path wins. Raises RuntimeError if nothing is found.
    """
    if profile.required_firmware is not None:
        if profile.required_firmware.exists():
            return profile.required_firmware
        raise RuntimeError(
            f"Required {profile.name} UEFI firmware is missing: {profile.required_firmware}\n"
            "Run `mise run test:firmware` to fetch it. Homebrew's bundled "
            "edk2-stable202408 ASSERTs in rEFInd across a warm reboot, wedging "
            "any role that reboots (hwe_kernel seed, reboot/kdump/console _verify)."
        )
    for c in profile.uefi_code_candidates:
        if Path(c).exists():
            return Path(c)
    raise RuntimeError(
        f"No {profile.name} UEFI firmware found in {list(profile.uefi_code_candidates)}. "
        "Install via `brew install qemu` (macOS), "
        "`apt install ovmf` / `apt install qemu-efi-aarch64` (Debian/Ubuntu), or "
        "`dnf install edk2-ovmf` (Fedora/RHEL)."
    )

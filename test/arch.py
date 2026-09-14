"""Per-architecture data driving the QEMU test harness.

Each supported host arch has a frozen profile consumed by machine.py. Adding
an arch means adding one profile constant plus a platform.machine() mapping.
"""

from __future__ import annotations

import dataclasses
import os
import platform
from pathlib import Path

_AARCH64_FIRMWARE_ENV = "HOMELAB_AARCH64_FIRMWARE_DIR"
_AARCH64_FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"


@dataclasses.dataclass(frozen=True)
class FirmwareRequirement:
    """A pinned CODE/VARS pair required instead of host-packaged firmware."""

    default_dir: Path
    directory_env: str
    code_name: str
    vars_name: str


@dataclasses.dataclass(frozen=True)
class ArchProfile:
    """Everything the test harness needs to know about a host arch.

    All fields are pure data; behaviour stays in the call sites that consume
    them. Frozen so an instance can be safely shared across Machines.
    """

    name: str
    qemu_binary: str
    machine_type: str
    net_device: str
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
    # A CODE/VARS pair the harness requires over any system-provided firmware.
    # None = use the CODE candidate search and synthesize a blank VARS file.
    required_firmware: FirmwareRequirement | None = None


X86_64 = ArchProfile(
    name="x86_64",
    qemu_binary="qemu-system-x86_64",
    machine_type="q35",
    net_device="virtio-net",
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
    # Ubuntu's ARM qemu package omits the optional virtio EFI ROM. The guest
    # firmware discovers PCI devices directly, so no ROM is needed.
    net_device="virtio-net,romfile=",
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
    # Homebrew's QEMU and Ubuntu Noble both package edk2-stable202408, whose DXE
    # allocator ASSERTs when rEFInd warm-reboots. Keep the newer pin mandatory
    # on every aarch64 host rather than silently accepting the packaged blobs.
    uefi_code_candidates=(),
    bios_boot_supported=False,
    required_firmware=FirmwareRequirement(
        default_dir=_AARCH64_FIRMWARE_DIR,
        directory_env=_AARCH64_FIRMWARE_ENV,
        code_name="edk2-aarch64-code.fd",
        vars_name="edk2-aarch64-vars.fd",
    ),
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


def uefi_firmware_paths_for(profile: ArchProfile) -> tuple[Path, Path | None]:
    """Locate the EDK2/OVMF CODE and optional VARS template for *profile*.

    Required firmware can be relocated as one directory through its environment
    override. Both members must exist; otherwise fail with fetch guidance rather
    than falling back to an older host package. Architectures without a pin use
    the first existing CODE candidate and let the harness create blank VARS.
    """
    if profile.required_firmware is not None:
        requirement = profile.required_firmware
        directory = Path(os.environ.get(requirement.directory_env, requirement.default_dir))
        code_path = directory / requirement.code_name
        vars_path = directory / requirement.vars_name
        missing = [path for path in (code_path, vars_path) if not path.is_file()]
        if missing:
            raise RuntimeError(
                f"Required {profile.name} UEFI firmware is missing: {', '.join(map(str, missing))}\n"
                f"Run `mise run test:firmware` to fetch it, or set {requirement.directory_env} "
                "to the directory containing the pinned CODE and VARS files. Older packaged "
                "edk2 builds ASSERT in rEFInd across a warm reboot."
            )
        return code_path, vars_path
    for c in profile.uefi_code_candidates:
        if Path(c).exists():
            return Path(c), None
    raise RuntimeError(
        f"No {profile.name} UEFI firmware found in {list(profile.uefi_code_candidates)}. "
        "Install via `brew install qemu` (macOS), "
        "`apt install ovmf` / `apt install qemu-efi-aarch64` (Debian/Ubuntu), or "
        "`dnf install edk2-ovmf` (Fedora/RHEL)."
    )

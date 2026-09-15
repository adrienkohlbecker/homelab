"""Per-architecture data driving the QEMU test harness.

Each supported host arch has a frozen profile consumed by machine.py. Guest
facts the Packer fixture build also needs come from data/architectures.yml.
Adding an arch means adding its data entry, one profile constant, and a
platform.machine() mapping.
"""

from __future__ import annotations

import dataclasses
import os
import platform
from pathlib import Path

import yaml

_ARCHITECTURES = yaml.safe_load((Path(__file__).resolve().parents[1] / "data" / "architectures.yml").read_text())
_X86_64_GUEST = _ARCHITECTURES["x86_64"]["guest"]
_AARCH64_GUEST = _ARCHITECTURES["aarch64"]["guest"]


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
    # Ordered candidate paths for the packaged EDK2/OVMF CODE blob. First
    # existing path wins.
    uefi_code_candidates: tuple[str, ...]
    # x86_64's q35 falls back to SeaBIOS off the OS disk, so the cloud-image
    # minimal variant doesn't need UEFI pflash. aarch64 virt only boots via
    # UEFI -- pflash must be attached even on minimal.
    bios_boot_supported: bool
    # (CODE, VARS) file names of the pinned pair `mise run test:firmware`
    # installs, required over any packaged firmware. None = use the CODE
    # candidate search and synthesize a blank VARS file.
    pinned_firmware: tuple[str, str] | None = None


X86_64 = ArchProfile(
    name="x86_64",
    qemu_binary="qemu-system-x86_64",
    machine_type=_X86_64_GUEST["machine_type"],
    net_device=_X86_64_GUEST["net_device"],
    cloud_image_suffix=_X86_64_GUEST["cloud_image_suffix"],
    serial_console_token="console=ttyS",
    serial_console_default="console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200",
    keep_vm_extra_devices=("-device", "usb-tablet"),
    # x86_64 harness hosts are Ubuntu KVM runners (ovmf package). Ubuntu 24.04
    # dropped the legacy non-4M OVMF_CODE.fd in favour of the 4M variant;
    # older releases still ship the legacy name. Try both.
    uefi_code_candidates=(
        "/usr/share/OVMF/OVMF_CODE_4M.fd",
        "/usr/share/OVMF/OVMF_CODE.fd",
    ),
    bios_boot_supported=True,
)


AARCH64 = ArchProfile(
    name="aarch64",
    qemu_binary="qemu-system-aarch64",
    machine_type=_AARCH64_GUEST["machine_type"],
    net_device=_AARCH64_GUEST["net_device"],
    cloud_image_suffix=_AARCH64_GUEST["cloud_image_suffix"],
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
    pinned_firmware=(_AARCH64_GUEST["firmware"]["code_name"], _AARCH64_GUEST["firmware"]["vars_name"]),
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

    A pinned pair lives in HOMELAB_AARCH64_FIRMWARE_DIR (default test/firmware,
    the same default Packer uses). Both members must exist; otherwise fail with
    fetch guidance rather than falling back to an older host package.
    Architectures without a pin use the first existing packaged CODE candidate
    and let the harness create blank VARS.
    """
    if profile.pinned_firmware is not None:
        directory = Path(os.environ.get("HOMELAB_AARCH64_FIRMWARE_DIR") or Path(__file__).resolve().parent / "firmware")
        code_path, vars_path = (directory / name for name in profile.pinned_firmware)
        missing = [path for path in (code_path, vars_path) if not path.is_file()]
        if missing:
            raise RuntimeError(
                f"Required {profile.name} UEFI firmware is missing: {', '.join(map(str, missing))}\n"
                "Run `mise run test:firmware` to fetch it, or set HOMELAB_AARCH64_FIRMWARE_DIR "
                "to the directory containing the pinned CODE and VARS files. Older packaged "
                "edk2 builds ASSERT in rEFInd across a warm reboot."
            )
        return code_path, vars_path
    for c in profile.uefi_code_candidates:
        if Path(c).exists():
            return Path(c), None
    raise RuntimeError(
        f"No {profile.name} UEFI firmware found in {list(profile.uefi_code_candidates)}. "
        "Install the `ovmf` package (Debian/Ubuntu)."
    )

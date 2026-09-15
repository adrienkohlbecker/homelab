"""Unit tests for test/arch.py — architecture profiles and detection."""

from pathlib import Path
from unittest import mock

import arch
import pytest


class TestProfiles:
    def test_x86_64_fields(self) -> None:
        p = arch.X86_64
        assert p.name == "x86_64"
        assert p.qemu_binary == "qemu-system-x86_64"
        assert p.machine_type == "q35"
        assert p.cloud_image_suffix == "amd64"
        assert p.bios_boot_supported is True

    def test_aarch64_fields(self) -> None:
        p = arch.AARCH64
        assert p.name == "aarch64"
        assert p.qemu_binary == "qemu-system-aarch64"
        assert p.machine_type == "virt"
        assert p.cloud_image_suffix == "arm64"
        assert p.bios_boot_supported is False

    def test_aarch64_has_more_keep_vm_devices(self) -> None:
        assert len(arch.AARCH64.keep_vm_extra_devices) > len(arch.X86_64.keep_vm_extra_devices)

    def test_aarch64_requires_the_pinned_firmware_pair(self) -> None:
        assert arch.AARCH64.pinned_firmware == ("edk2-aarch64-code.fd", "edk2-aarch64-vars.fd")
        assert arch.X86_64.pinned_firmware is None
        assert arch.AARCH64.net_device == "virtio-net,romfile="

    def test_every_shared_architecture_has_a_harness_profile(self) -> None:
        # Packer and the CI stores accept any architecture in the data file; the
        # harness must be able to boot each one.
        assert set(arch._ARCHITECTURES) == {profile.name for profile in arch._BY_PLATFORM_MACHINE.values()}

    def test_profiles_are_frozen(self) -> None:
        with pytest.raises(AttributeError):
            arch.X86_64.name = "changed"  # type: ignore[misc]


class TestDetectHostArch:
    def test_x86_64(self) -> None:
        with mock.patch.object(arch.platform, "machine", return_value="x86_64"):
            assert arch.detect_host_arch() is arch.X86_64

    def test_amd64_normalizes(self) -> None:
        with mock.patch.object(arch.platform, "machine", return_value="amd64"):
            assert arch.detect_host_arch() is arch.X86_64

    def test_aarch64(self) -> None:
        with mock.patch.object(arch.platform, "machine", return_value="aarch64"):
            assert arch.detect_host_arch() is arch.AARCH64

    def test_arm64_normalizes(self) -> None:
        with mock.patch.object(arch.platform, "machine", return_value="arm64"):
            assert arch.detect_host_arch() is arch.AARCH64

    def test_unknown_raises(self) -> None:
        with (
            mock.patch.object(arch.platform, "machine", return_value="riscv64"),
            pytest.raises(RuntimeError, match="Unsupported"),
        ):
            arch.detect_host_arch()


class TestUefiFirmwarePaths:
    def test_finds_first_existing(self, tmp_path: Path) -> None:
        profile = arch.ArchProfile(
            name="test",
            qemu_binary="qemu-system-test",
            machine_type="virt",
            net_device="virtio-net",
            cloud_image_suffix="test",
            serial_console_token="console=tty",
            serial_console_default="console=tty0",
            keep_vm_extra_devices=(),
            uefi_code_candidates=(
                str(tmp_path / "nonexistent.fd"),
                str(tmp_path / "found.fd"),
                str(tmp_path / "also_found.fd"),
            ),
            bios_boot_supported=False,
        )
        (tmp_path / "found.fd").write_bytes(b"uefi")
        (tmp_path / "also_found.fd").write_bytes(b"uefi2")
        assert arch.uefi_firmware_paths_for(profile) == (tmp_path / "found.fd", None)

    def test_raises_when_none_exist(self) -> None:
        profile = arch.ArchProfile(
            name="test",
            qemu_binary="qemu-system-test",
            machine_type="virt",
            net_device="virtio-net",
            cloud_image_suffix="test",
            serial_console_token="console=tty",
            serial_console_default="console=tty0",
            keep_vm_extra_devices=(),
            uefi_code_candidates=("/nonexistent/a.fd", "/nonexistent/b.fd"),
            bios_boot_supported=False,
        )
        with pytest.raises(RuntimeError, match="No test UEFI firmware"):
            arch.uefi_firmware_paths_for(profile)

    @staticmethod
    def _pinned_profile() -> arch.ArchProfile:
        return arch.ArchProfile(
            name="test",
            qemu_binary="qemu-system-test",
            machine_type="virt",
            net_device="virtio-net",
            cloud_image_suffix="test",
            serial_console_token="console=tty",
            serial_console_default="console=tty0",
            keep_vm_extra_devices=(),
            uefi_code_candidates=(),
            bios_boot_supported=False,
            pinned_firmware=("code.fd", "vars.fd"),
        )

    def test_pinned_pair_uses_directory_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "code.fd").write_bytes(b"code")
        (tmp_path / "vars.fd").write_bytes(b"vars")
        monkeypatch.setenv("HOMELAB_AARCH64_FIRMWARE_DIR", str(tmp_path))

        assert arch.uefi_firmware_paths_for(self._pinned_profile()) == (tmp_path / "code.fd", tmp_path / "vars.fd")

    def test_pinned_pair_defaults_to_the_fetched_firmware_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Empty matches Packer's env() default and must not mean the cwd.
        monkeypatch.setenv("HOMELAB_AARCH64_FIRMWARE_DIR", "")
        default_dir = Path(arch.__file__).resolve().parent / "firmware"

        with pytest.raises(RuntimeError, match=str(default_dir / "code.fd")):
            arch.uefi_firmware_paths_for(self._pinned_profile())

    def test_pinned_pair_rejects_missing_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "code.fd").write_bytes(b"code")
        monkeypatch.setenv("HOMELAB_AARCH64_FIRMWARE_DIR", str(tmp_path))

        with pytest.raises(RuntimeError, match=r"vars\.fd"):
            arch.uefi_firmware_paths_for(self._pinned_profile())

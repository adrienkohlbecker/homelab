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


class TestUefiCodePath:
    def test_finds_first_existing(self, tmp_path: Path) -> None:
        profile = arch.ArchProfile(
            name="test",
            qemu_binary="qemu-system-test",
            machine_type="virt",
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
        assert arch.uefi_code_path_for(profile) == tmp_path / "found.fd"

    def test_raises_when_none_exist(self) -> None:
        profile = arch.ArchProfile(
            name="test",
            qemu_binary="qemu-system-test",
            machine_type="virt",
            cloud_image_suffix="test",
            serial_console_token="console=tty",
            serial_console_default="console=tty0",
            keep_vm_extra_devices=(),
            uefi_code_candidates=("/nonexistent/a.fd", "/nonexistent/b.fd"),
            bios_boot_supported=False,
        )
        with pytest.raises(RuntimeError, match="No test UEFI firmware"):
            arch.uefi_code_path_for(profile)

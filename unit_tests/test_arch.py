"""Unit tests for test/arch.py — architecture profiles and detection."""

from pathlib import Path
from unittest import mock

import arch
import pytest


class TestProfiles:
    def test_every_shared_architecture_has_a_harness_profile(self) -> None:
        # Packer and the CI stores accept any architecture in the data file; the
        # harness must be able to boot each one.
        assert set(arch._ARCHITECTURES) == {profile.name for profile in arch._BY_PLATFORM_MACHINE.values()}


class TestDetectHostArch:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [("x86_64", arch.X86_64), ("amd64", arch.X86_64), ("aarch64", arch.AARCH64), ("arm64", arch.AARCH64)],
    )
    def test_normalizes_platform_machine(self, machine: str, expected: arch.ArchProfile) -> None:
        with mock.patch.object(arch.platform, "machine", return_value=machine):
            assert arch.detect_host_arch() is expected

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
        assert arch.uefi_code_path_for(profile) == tmp_path / "found.fd"

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
            arch.uefi_code_path_for(profile)

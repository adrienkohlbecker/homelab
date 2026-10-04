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
    @pytest.mark.parametrize(("system", "host_os"), [("Linux", "linux"), ("Darwin", "darwin")])
    def test_selects_the_host_os_pair(self, system: str, host_os: str) -> None:
        with mock.patch.object(arch.platform, "system", return_value=system):
            assert arch.uefi_code_path_for(arch.AARCH64) == Path(arch.AARCH64.uefi_firmware[host_os]["code"])

    def test_raises_for_an_unsupported_host_os(self) -> None:
        with (
            mock.patch.object(arch.platform, "system", return_value="Darwin"),
            pytest.raises(RuntimeError, match="No x86_64 UEFI firmware is defined for darwin hosts"),
        ):
            arch.uefi_code_path_for(arch.X86_64)

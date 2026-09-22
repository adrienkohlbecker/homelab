"""Exercise the kexec wrapper that the ZBM early-setup hook installs on aarch64."""

from __future__ import annotations

import gzip
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "zbm" / "hooks" / "early-setup.d" / "40-kexec-uefi-secure-boot.sh"
IMAGE = b"MZ\x00\x00" + b"arm64 image payload " * 500


def _zboot(payload: bytes, kind: bytes) -> bytes:
    """A PE-wrapped zboot image: an outer PE, then the inner MZ stub, zimg header and payload."""
    header = b"MZ\x00\x00zimg" + struct.pack("<II", 64, len(payload)) + b"\0" * 8 + kind.ljust(32, b"\0")
    return b"MZ\0\0outer pe sections\0\0" + header.ljust(64, b"\0") + payload + b"trailing signature"


def _wrapper(tmp_path: Path) -> tuple[Path, Path]:
    """Extract the wrapper from the hook, pointed at a fake kexec that records its argv."""
    script = HOOK.read_text().split("<<'WRAPPER'\n", 1)[1].split("\nWRAPPER", 1)[0]
    real = tmp_path / "kexec.real"
    real.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$0.argv"\n')
    real.chmod(0o755)
    wrapper = tmp_path / "kexec"
    wrapper.write_text(
        script.replace("/usr/bin/kexec.real", str(real))
        .replace("/run/kexec_kernel_Image", str(tmp_path / "Image"))
        .replace("/run/zbm_kexec.dtb", "DTB")
    )
    wrapper.chmod(0o755)
    return wrapper, Path(f"{real}.argv")


def _run(wrapper: Path, argv_file: Path, *args: str) -> list[str]:
    subprocess.run(["bash", str(wrapper), *args], check=True)
    return argv_file.read_text().splitlines()


def _zstd(data: bytes) -> bytes:
    return subprocess.run(["zstd", "-c"], input=data, check=True, capture_output=True).stdout


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is required")
def test_load_unwraps_a_zstd_zboot_kernel(tmp_path: Path) -> None:
    wrapper, argv_file = _wrapper(tmp_path)
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(_zboot(_zstd(IMAGE), b"zstd22"))

    argv = _run(wrapper, argv_file, "-a", "-l", str(kernel), "--initrd=/initrd", "--command-line=root=x")

    assert argv == [
        "--kexec-syscall",
        "--no-checks",
        "-l",
        str(tmp_path / "Image"),
        "--initrd=/initrd",
        "--command-line=root=x",
        "--dtb=DTB",
    ]
    assert (tmp_path / "Image").read_bytes() == IMAGE


def test_load_unwraps_a_gzip_zboot_kernel(tmp_path: Path) -> None:
    wrapper, argv_file = _wrapper(tmp_path)
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(_zboot(gzip.compress(IMAGE), b"gzip"))

    argv = _run(wrapper, argv_file, "-l", str(kernel))

    assert argv[:4] == ["--kexec-syscall", "--no-checks", "-l", str(tmp_path / "Image")]
    assert (tmp_path / "Image").read_bytes() == IMAGE


def test_load_passes_other_kernels_through(tmp_path: Path) -> None:
    wrapper, argv_file = _wrapper(tmp_path)
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(gzip.compress(IMAGE))

    argv = _run(wrapper, argv_file, "-a", "-l", str(kernel))

    assert argv == ["--kexec-syscall", "--no-checks", "-l", str(kernel), "--dtb=DTB"]
    assert not (tmp_path / "Image").exists()


def test_non_load_invocations_are_untouched(tmp_path: Path) -> None:
    wrapper, argv_file = _wrapper(tmp_path)

    assert _run(wrapper, argv_file, "-e", "-i") == ["-e", "-i"]

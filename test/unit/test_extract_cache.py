"""Cache-hit path for KernelInitrdExtractor.

Cache-miss spawns a real qemu and is integration-level; covered by the
existing testrole.py end-to-end runs. The cache-hit path is fully
deterministic and worth pinning down here -- it's the contract every other
testrole.py invocation hits after the first packer build.
"""

import asyncio
from pathlib import Path

import extract
from arch import X86_64


def test_extract_returns_cached_artifacts_without_running_qemu(
    tmp_path: Path,
) -> None:
    imagedir = tmp_path
    src = imagedir / "packer-ubuntu-1"
    src.write_bytes(b"fake-qcow2-bytes")

    extractor = extract.KernelInitrdExtractor(
        imagedir=imagedir,
        ubuntu_name="jammy",
        os_src_paths=[str(src)],
        arch=X86_64,
    )

    # Pre-populate the cache so extract() must take the cache-hit branch;
    # if it tried to spawn qemu we'd notice the test taking minutes to fail.
    extractor.cache_dir.mkdir(parents=True)
    extractor.kernel_path.write_bytes(b"vmlinuz-cached")
    extractor.initrd_path.write_bytes(b"initrd-cached")
    extractor.cmdline_path.write_text("root=zfs=rpool/ROOT/cached extra=arg\n")

    kernel, initrd, cmdline = asyncio.run(extractor.extract())

    assert kernel == extractor.kernel_path
    assert initrd == extractor.initrd_path
    # cmdline trailing whitespace is stripped per the contract.
    assert cmdline == "root=zfs=rpool/ROOT/cached extra=arg"


def test_cache_dir_is_keyed_by_qcow2_fingerprint(tmp_path: Path) -> None:
    src = tmp_path / "packer-ubuntu-1"
    src.write_bytes(b"AAA")

    extractor = extract.KernelInitrdExtractor(
        imagedir=tmp_path,
        ubuntu_name="jammy",
        os_src_paths=[str(src)],
        arch=X86_64,
    )

    # The cache directory's basename is exactly the sha256 of the qcow2
    # contents, so a packer rebuild (different bytes) auto-invalidates.
    assert extractor.cache_dir.name == extract._qcow2_fingerprint([src])
    assert extractor.cache_dir.parent == tmp_path / "extracted"


def test_different_arch_does_not_change_cache_key(tmp_path: Path) -> None:
    """Cache key is content-only; arch is metadata for the extraction VM."""
    from arch import AARCH64

    src = tmp_path / "packer-ubuntu-1"
    src.write_bytes(b"some-bytes")

    x86 = extract.KernelInitrdExtractor(
        imagedir=tmp_path, ubuntu_name="jammy",
        os_src_paths=[str(src)], arch=X86_64,
    )
    arm = extract.KernelInitrdExtractor(
        imagedir=tmp_path, ubuntu_name="jammy",
        os_src_paths=[str(src)], arch=AARCH64,
    )
    # Same fingerprint; different arches share the cache directory. The
    # cmdline produced by the extraction VM is what's arch-specific, and
    # that's already inside the cmdline file the caller composes from.
    assert x86.cache_dir == arm.cache_dir

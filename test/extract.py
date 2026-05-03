"""KernelInitrdExtractor: pull on-pool kernel + initrd out of a ZFS-rooted packer qcow2.

Used on aarch64, where the rEFInd -> ZBM -> kexec chain in the packer image
panics on EDK2 (see notes/zbm-aarch64-kexec-bug-report.md). Spins up a
one-shot Ubuntu cloud-image VM that apt-installs zfsutils-linux, imports
the rpool from the attached packer qcow2(s), and copies the highest-
versioned vmlinuz + initrd to a 9p host share. Subsequent runs cache-hit
until the source qcow2 sha256 changes (i.e. packer rebuilt).

The cache returns (kernel, initrd, full_cmdline) where full_cmdline is
"root=zfs=<bootfs> <org.zfsbootmenu:commandline>" -- composed inside the
extraction VM by reading the ZBM property off rpool/ROOT, matching how
ZBM itself builds the kexec cmdline. The caller backfills console=/
earlycon= defaults if the property doesn't supply them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import hashlib
import platform
import shutil
import tempfile
from pathlib import Path

from arch import ArchProfile, uefi_code_path_for
from utils import print_cmd_line, print_line, run_command


# Cloud-init script consumed by the one-shot extraction VM. Lives in its
# own file so editor YAML highlighting / yamllint pick it up; see the
# header comment in test/extraction/user-data for what it does.
EXTRACTION_USER_DATA_PATH = Path(__file__).parent / "extraction" / "user-data"

# 20 minutes covers a cold cloud-image apt update + install + zpool import
# on a slow link. Cache hits skip all of this.
_QEMU_TIMEOUT_SECONDS = 20 * 60


def _qcow2_fingerprint(paths: list[Path]) -> str:
    """sha256 over the OS qcow2(s) -- cache key for extracted kernel/initrd.

    Reads all bytes; on a 1 GiB packer image this takes a few seconds. Sorted
    by path so a multi-disk variant (ubuntu-zfs-lab's 3-way mirror) yields a
    stable digest regardless of iteration order. Any packer rebuild changes
    the qcow2 contents, which invalidates the cache and re-runs extraction.
    """
    h = hashlib.sha256()
    for p in sorted(paths):
        with p.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
    return h.hexdigest()


async def _build_seed_iso(out: Path, user_data: Path, meta_data: Path) -> None:
    """Pack a NoCloud cidata seed iso for the extraction VM's cloud-init."""
    if shutil.which("cloud-localds"):
        await run_command(["cloud-localds", str(out), str(user_data), str(meta_data)])
        return
    iso_tool = shutil.which("xorrisofs") or shutil.which("mkisofs") or shutil.which("genisoimage")
    if iso_tool is None:
        raise RuntimeError("Need cloud-localds or xorrisofs/mkisofs/genisoimage in PATH to build cloud-init seed iso")
    await run_command(
        [
            iso_tool,
            "-output",
            str(out),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(user_data),
            str(meta_data),
        ]
    )


@dataclasses.dataclass
class KernelInitrdExtractor:
    """One-shot extraction VM, cached by sha256 of the OS qcow2 contents."""

    imagedir: str
    ubuntu_name: str
    os_src_paths: list[str]
    arch: ArchProfile
    # When False, fetch the upstream Ubuntu cloud image through the lab
    # Nexus raw proxy (matches the same flag's semantics for ansible
    # mirrors and packer apt). True bypasses to cloud-images.ubuntu.com.
    upstream_mirrors: bool = False

    fingerprint: str = dataclasses.field(init=False)
    cache_dir: Path = dataclasses.field(init=False)
    kernel_path: Path = dataclasses.field(init=False)
    initrd_path: Path = dataclasses.field(init=False)
    cmdline_path: Path = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.fingerprint = _qcow2_fingerprint([Path(p) for p in self.os_src_paths])
        self.cache_dir = Path(self.imagedir) / "extracted" / self.fingerprint
        self.kernel_path = self.cache_dir / "kernel"
        self.initrd_path = self.cache_dir / "initrd"
        self.cmdline_path = self.cache_dir / "cmdline"

    async def extract(self) -> tuple[Path, Path, str]:
        """Return cached (kernel, initrd, cmdline) or run the extraction VM once."""
        cached = self._read_cache()
        if cached is not None:
            return cached
        return await self._extract_under_lock()

    def _read_cache(self) -> tuple[Path, Path, str] | None:
        if self.kernel_path.exists() and self.initrd_path.exists() and self.cmdline_path.exists():
            return self.kernel_path, self.initrd_path, self.cmdline_path.read_text().strip()
        return None

    async def _extract_under_lock(self) -> tuple[Path, Path, str]:
        # Serialise concurrent testrole.py workers extracting the same
        # qcow2: without the lock, parallel testall.py would spin up two
        # extraction VMs on the same fingerprint and race on the copy
        # into cache_dir/.
        self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_dir.parent / f"{self.fingerprint}.lock"
        with lock_path.open("w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Re-check inside the lock: the previous holder may have just
            # finished extracting; cache-hit instead of redoing the work.
            cached = self._read_cache()
            if cached is not None:
                return cached

            print_line(
                f"Extracting kernel/initrd from packer qcow2 (cache miss; sha256={self.fingerprint[:12]})"
            )
            cloud_image = await self._ensure_cloudimg()

            with tempfile.TemporaryDirectory(dir=self.imagedir) as tmpdir:
                tmp = Path(tmpdir)
                seed = await self._build_seed(tmp)
                os_overlay = await self._build_cloudimg_overlay(tmp, cloud_image)
                rpool_overlays = await self._build_rpool_overlays(tmp)
                share = tmp / "share"
                share.mkdir()

                cmd = await self._build_qemu_cmd(tmp, os_overlay, seed, rpool_overlays, share)
                log_path = tmp / "extract.log"
                await self._run_qemu(cmd, log_path)

                if not (share / "done").exists():
                    # Promote the qemu log out of tmpdir so the user can
                    # inspect it after TemporaryDirectory cleanup runs.
                    fail_log = self.cache_dir.parent / f"{self.fingerprint}.failed.log"
                    with contextlib.suppress(OSError):
                        shutil.copy2(log_path, fail_log)
                    raise RuntimeError(f"Kernel extraction failed; see {fail_log}")

                self._commit_to_cache(share)

        return self.kernel_path, self.initrd_path, self.cmdline_path.read_text().strip()

    async def _ensure_cloudimg(self) -> Path:
        """Download (once) the Ubuntu cloud image used as the extraction vehicle.

        Pulls through the lab Nexus raw proxy by default; upstream_mirrors=True
        bypasses to cloud-images.ubuntu.com directly.
        """
        name = f"{self.ubuntu_name}-server-cloudimg-{self.arch.cloud_image_suffix}.img"
        cache = Path(self.imagedir) / "cloud-images"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.exists():
            return target

        base = "https://cloud-images.ubuntu.com" if self.upstream_mirrors else "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images"
        url = f"{base}/{self.ubuntu_name}/current/{name}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        print_line(f"Downloading {url}")
        await run_command(["curl", "-fL", "--retry", "3", "-o", str(tmp), url])
        tmp.rename(target)
        return target

    async def _build_seed(self, tmp: Path) -> Path:
        (tmp / "meta-data").write_text("instance-id: extract\nlocal-hostname: extract\n")
        seed = tmp / "seed.iso"
        await _build_seed_iso(seed, EXTRACTION_USER_DATA_PATH, tmp / "meta-data")
        return seed

    async def _build_cloudimg_overlay(self, tmp: Path, cloud_image: Path) -> Path:
        """Writeable overlay of the cloud image, resized so apt has headroom."""
        os_overlay = tmp / "cloud.qcow2"
        await run_command(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-b",
                str(cloud_image),
                "-F",
                "qcow2",
                str(os_overlay),
                "20G",
            ]
        )
        return os_overlay

    async def _build_rpool_overlays(self, tmp: Path) -> list[Path]:
        """Per-disk overlays of the source packer qcow2(s).

        Imports through these so the originals never get mutated and a
        crashed extraction can't corrupt them.
        """
        overlays: list[Path] = []
        for idx, src in enumerate(self.os_src_paths, start=1):
            overlay = tmp / f"rpool-{idx}.qcow2"
            await run_command(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    str(Path(src).resolve()),
                    "-F",
                    "qcow2",
                    str(overlay),
                ]
            )
            overlays.append(overlay)
        return overlays

    async def _build_qemu_cmd(
        self,
        tmp: Path,
        os_overlay: Path,
        seed: Path,
        rpool_overlays: list[Path],
        share: Path,
    ) -> list[str]:
        accel = "hvf" if platform.system() == "Darwin" else "kvm"
        cmd: list[str] = [
            self.arch.qemu_binary,
            "--drive",
            f"file={os_overlay},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
            "--drive",
            f"file={seed},if=virtio,format=raw",
            *(arg for ov in rpool_overlays for arg in ("--drive", f"file={ov},if=virtio,format=qcow2,cache=unsafe")),
            "-netdev",
            "user,id=net0",
            "-device",
            "virtio-net,netdev=net0",
            "-fsdev",
            f"local,id=share,path={share},security_model=mapped-xattr",
            "-device",
            "virtio-9p-pci,fsdev=share,mount_tag=share",
            "-machine",
            f"type={self.arch.machine_type},accel={accel}",
            "-cpu",
            "host",
            "-smp",
            "4",
            "-m",
            "2048M",
            "-display",
            "none",
            "-serial",
            "null",
            "-no-reboot",
        ]
        if not self.arch.bios_boot_supported:
            code_path = uefi_code_path_for(self.arch)
            vars_path = tmp / "AAVMF_VARS.fd"
            # Size empty vars from the code blob so pflash pair sizes match.
            await run_command(["truncate", "-s", str(code_path.stat().st_size), str(vars_path)])
            cmd += [
                "-drive",
                f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
                "-drive",
                f"file={vars_path},if=pflash,unit=1,format=raw",
            ]
        return cmd

    async def _run_qemu(self, cmd: list[str], log_path: Path) -> None:
        print_cmd_line(cmd)
        with log_path.open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=_QEMU_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise RuntimeError(f"Kernel extraction timed out (log: {log_path})") from None

    def _commit_to_cache(self, share: Path) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(share / "kernel", self.kernel_path)
        shutil.copy2(share / "initrd", self.initrd_path)
        shutil.copy2(share / "cmdline", self.cmdline_path)

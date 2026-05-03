#!/usr/bin/env -S uv run
"""Launch a QEMU machine via the test harness driver, no role/ansible.

Wraps machine.QemuMachine for interactive use: pick a variant, the harness
does image overlays + (on aarch64 ZFS) on-pool kernel extraction + qemu
launch. After boot it prints the SSH command, leaves the VM up, and blocks
until Ctrl-C.

Replaces the ad-hoc test.sh for ZBM iteration. Pass --kernel/--initrd/
--append to direct-boot a custom kernel against the variant's qcow2:

  test/launch.py --machine box \\
      --kernel zbm-build/aarch64/.../vmlinux-bootmenu \\
      --initrd zbm-build/aarch64/.../initramfs-bootmenu.img \\
      --append 'earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200 zbm.show' \\
      --no-ssh-wait --foreground
"""

import argparse
import asyncio
import contextlib
import signal
import subprocess
import sys
from pathlib import Path

import machine as machine_mod
from machine import (
    DEFAULT_UBUNTU,
    QEMU_MACHINE_SPECS,
    QemuMachine,
    UBUNTU_RELEASES,
)
from utils import cancel_on_signal, print_cmd_line, print_line, run_command, tee_output


class _LaunchMachine(QemuMachine):
    """QemuMachine with launch.py-only knobs layered on top.

    Adds:
    - direct-boot override (kernel/initrd/append) -- skips _extract_kernel_initrd
      so the aarch64 ZFS path doesn't run a 5+ min extraction we'd discard
    - --mem override
    - --with-pflash / --efi-code / --efi-vars to attach UEFI pflash with
      either auto-detected paths or explicit user-supplied ones
    - repeatable --virtfs PATH:TAG 9p host shares
    - --foreground: inherit qemu stdio + replace `-serial stdio` with mon:stdio
      so HMP is reachable via Ctrl-A,c
    - --qmp SOCKET: bind qemu's QMP server to a unix socket
    """

    def __init__(
        self,
        *args: object,
        kernel: Path | None = None,
        initrd: Path | None = None,
        append: str = "",
        mem: str | None = None,
        with_pflash: bool = False,
        efi_code: Path | None = None,
        efi_vars: Path | None = None,
        virtfs: list[tuple[Path, str]] | None = None,
        foreground: bool = False,
        qmp_socket: Path | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._direct_boot_override: tuple[Path, Path, str] | None = (
            (kernel.resolve(), initrd.resolve(), append) if kernel is not None else None
        )
        self._mem = mem
        self._with_pflash = with_pflash
        self._efi_code = efi_code.resolve() if efi_code is not None else None
        self._efi_vars = efi_vars.resolve() if efi_vars is not None else None
        self._virtfs = list(virtfs or [])
        self._foreground = foreground
        self._qmp_socket = qmp_socket

    async def _uefi_drives(self) -> list[str]:
        """Honour --efi-code / --efi-vars overrides, else delegate to parent.

        Parent's _uefi_drives is also used by super().prepare() for ZFS /
        minimal-aarch64 firmware boots, so this override flows through to
        those paths automatically (Python MRO) -- letting the user supply
        a custom OVMF / EDK2 build for any pflash-using variant.
        """
        if self._efi_code is None and self._efi_vars is None:
            return await super()._uefi_drives()

        code_path = self._efi_code if self._efi_code is not None else machine_mod._uefi_code_path(self.host_arch)
        if self._efi_vars is not None:
            vars_path = self._efi_vars
        else:
            # Mirrors super()._uefi_drives logic: prefer packer-shipped
            # efivars (preserves boot order across runs) before creating an
            # empty file sized to the code blob.
            packer_vars = Path(f"{self.workdir.name}/efivars.fd")
            if packer_vars.exists():
                vars_path = packer_vars
            else:
                vars_path = Path(f"{self.workdir.name}/uefi-vars.fd")
                await run_command(["truncate", "-s", str(code_path.stat().st_size), str(vars_path)])
        return [
            f"file={code_path},if=pflash,unit=0,format=raw,readonly=on",
            f"file={vars_path},if=pflash,unit=1,format=raw",
        ]

    async def prepare(self) -> None:
        if self._direct_boot_override is not None:
            override = self._direct_boot_override

            # Stub the module-level extractor so super().prepare() on aarch64
            # ZFS doesn't run the cloud-image extraction; we'd throw away
            # the result anyway.
            async def stub(**_kw: object) -> tuple[Path, Path, str]:
                return override

            orig = machine_mod._extract_kernel_initrd
            machine_mod._extract_kernel_initrd = stub  # type: ignore[assignment]
            try:
                await super().prepare()
            finally:
                machine_mod._extract_kernel_initrd = orig  # type: ignore[assignment]
            # x86_64 prepare() never calls the extractor; set _direct_boot
            # here so _boot_command emits -kernel/-initrd/-append on both
            # arches when the user overrides.
            self._direct_boot = override
        else:
            await super().prepare()

        # Attach pflash on the variants that don't get it from super (aarch64
        # ZFS direct-boot, x86_64 minimal BIOS). Either --with-pflash or any
        # of the explicit path overrides triggers attachment. Idempotent:
        # skip when super().prepare() already attached pflash (x86_64 ZFS,
        # aarch64 minimal); the override flowed through there already.
        want_pflash = self._with_pflash or self._efi_code is not None or self._efi_vars is not None
        if want_pflash and not any("if=pflash" in d for d in self.drives):
            self.drives += await self._uefi_drives()

    def _boot_command(self) -> list[str]:
        cmd = super()._boot_command()

        if self._mem is not None:
            i = cmd.index("-m")
            cmd[i + 1] = self._mem

        if self._foreground:
            # Strip the `timeout --kill-after=10s 0 ...` wrapper that
            # QemuMachine prepends. GNU timeout, when not invoked directly
            # from a shell prompt, detaches the child from the controlling
            # tty (it has a `--foreground` flag specifically to opt out of
            # that). Without that flag qemu can't put the terminal into
            # raw mode, so mon:stdio is unusable. We don't need timeout in
            # interactive mode anyway -- the user quits via Ctrl-A,x.
            assert cmd[0] == "timeout", f"expected timeout wrapper, got {cmd[:4]}"
            cmd = cmd[3:]

            # mon:stdio multiplexes the guest's first serial port with qemu's
            # HMP. Press Ctrl-A,c at the terminal to switch to HMP, Ctrl-A,c
            # again to return; Ctrl-A,x to quit qemu.
            i = cmd.index("-serial")
            cmd[i + 1] = "mon:stdio"

        if self._qmp_socket is not None:
            cmd += ["-qmp", f"unix:{self._qmp_socket},server,nowait"]
        for path, tag in self._virtfs:
            cmd += [
                "-virtfs",
                f"local,id={tag},path={path},mount_tag={tag},security_model=mapped-xattr",
            ]
        return cmd

def _parse_virtfs(specs: list[str]) -> list[tuple[Path, str]]:
    """Parse PATH:TAG pairs from --virtfs flags."""
    out: list[tuple[Path, str]] = []
    for spec in specs:
        path, sep, tag = spec.rpartition(":")
        if not sep or not path or not tag:
            raise argparse.ArgumentTypeError(f"--virtfs expects PATH:TAG, got {spec!r}")
        out.append((Path(path).resolve(), tag))
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default="minimal",
        choices=sorted(QEMU_MACHINE_SPECS),
        help="QEMU machine variant",
    )
    parser.add_argument(
        "--ubuntu",
        default=DEFAULT_UBUNTU,
        choices=sorted(UBUNTU_RELEASES),
        help="Ubuntu release codename",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Force-shutdown after N seconds (0 = no timeout, default)",
    )
    parser.add_argument(
        "--kernel",
        type=Path,
        help="Override kernel for direct -kernel boot (also requires --initrd)",
    )
    parser.add_argument(
        "--initrd",
        type=Path,
        help="Override initrd (also requires --kernel)",
    )
    parser.add_argument(
        "--append",
        default="",
        help="Kernel cmdline (used with --kernel/--initrd). The harness "
        "auto-appends an arch-appropriate serial console= + earlycon= unless "
        "you already supplied one (ttyAMA on aarch64, ttyS on x86_64).",
    )
    parser.add_argument(
        "--mem",
        default=None,
        metavar="SIZE",
        help="qemu memory size, e.g. '8192' or '8G'. Default 4096M.",
    )
    parser.add_argument(
        "--with-pflash",
        action="store_true",
        help="Attach EDK2/OVMF UEFI pflash for either arch using auto-detected "
        "code (Homebrew / Linux distro paths) and an empty vars file sized "
        "to the code. Needed for kernels that expect EFI runtime services; "
        "the harness's default direct -kernel boot (aarch64 ZFS) and BIOS "
        "boot (x86_64 minimal) skip firmware. No-op when pflash is already "
        "attached (x86_64 ZFS, aarch64 minimal).",
    )
    parser.add_argument(
        "--efi-code",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the auto-detected UEFI CODE blob (e.g. a custom "
        "EDK2/OVMF build, secure-boot variant, etc.). Implies pflash "
        "attachment. Combine with --efi-vars to pin both halves; if vars "
        "is omitted, the harness creates an empty vars file sized to this "
        "code blob (or reuses the packer-shipped efivars.fd when present).",
    )
    parser.add_argument(
        "--efi-vars",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the EFI vars file (the writable half of the pflash "
        "pair). Implies pflash attachment. Combine with --efi-code to pin "
        "both halves; if code is omitted, the harness's auto-detected blob "
        "is used and this vars file must match its size.",
    )
    parser.add_argument(
        "--virtfs",
        action="append",
        default=[],
        metavar="PATH:TAG",
        help="Mount PATH on the host as a 9p share with mount_tag=TAG inside "
        "the guest (`mount -t 9p TAG /mnt`). Repeatable.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Inherit qemu's stdio and use -serial mon:stdio so HMP is "
        "reachable via Ctrl-A,c (Ctrl-A,x to quit). The boot log is NOT "
        "captured to a file; implies --no-ssh-wait.",
    )
    parser.add_argument(
        "--qmp",
        type=Path,
        default=None,
        metavar="SOCKET",
        help="Bind qemu's QMP server to the given unix socket path.",
    )
    parser.add_argument(
        "--no-ssh-wait",
        action="store_true",
        help="Skip the SSH ready-check after boot -- e.g. when launching ZBM "
        "or any payload that doesn't expose sshd",
    )

    args = parser.parse_args()
    if (args.kernel is None) != (args.initrd is None):
        parser.error("--kernel and --initrd must be provided together")
    try:
        args.virtfs = _parse_virtfs(args.virtfs)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


async def _run_async(m: QemuMachine, *, wait_for_ssh: bool) -> None:
    """Default flow: prepare + boot + ensure_ssh + wait, all under asyncio."""
    task = asyncio.current_task()
    assert task is not None
    with cancel_on_signal(task):
        async with m:
            await m.ensure_booted()
            print_line("Booted")
            if wait_for_ssh:
                try:
                    await m.ensure_ssh()
                    print_line("SSH up")
                    m.print_ssh_instructions()
                except TimeoutError as exc:
                    print_line(f"SSH did not come up in time: {exc}")
            await m.wait()


def _run_foreground(m: _LaunchMachine) -> int:
    """Sync qemu spawn -- no asyncio for the long-running wait.

    asyncio's subprocess machinery installs its own SIGCHLD/fd plumbing in
    the event loop, which can interact poorly with qemu's mon:stdio raw-
    terminal handling. In foreground mode we want qemu to behave exactly
    as if a shell exec'd it: inherited stdio, controlling tty intact, no
    intermediary readers competing for fd 0. So we run prepare() in a
    short-lived event loop, then drop out of asyncio entirely and use
    subprocess.Popen() to execute qemu and wait for it.

    Skips ensure_booted/ensure_ssh -- those are useful when the harness is
    driving an unattended boot, but in foreground the user *is* the
    monitor and the polling output (sleep_tick dots, status lines) would
    just clutter the qemu serial console.
    """
    try:
        asyncio.run(m.prepare())
        cmd = m._boot_command()
        print_cmd_line(cmd)
        proc = subprocess.Popen(cmd)
        try:
            return proc.wait() or 0
        except KeyboardInterrupt:
            # Ctrl-C only reaches us before mon:stdio engages raw mode (or
            # after qemu exits) -- in raw mode qemu intercepts it as a guest
            # keystroke. Forward to qemu just in case and wait it out.
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            proc.wait()
            return 130
    finally:
        # __aenter__ / __aexit__ aren't used here, so do the cleanup the
        # async-with would normally do.
        m.workdir.cleanup()


def main() -> int:
    args = parse_args()

    m = _LaunchMachine(
        machine=args.machine,
        role="_launch",
        keep_vm=True,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        kernel=args.kernel,
        initrd=args.initrd,
        append=args.append,
        mem=args.mem,
        with_pflash=args.with_pflash,
        efi_code=args.efi_code,
        efi_vars=args.efi_vars,
        virtfs=args.virtfs,
        foreground=args.foreground,
        qmp_socket=args.qmp,
    )

    if args.foreground:
        return _run_foreground(m)

    rc = 0
    try:
        with tee_output(m.output_file):
            asyncio.run(_run_async(m, wait_for_ssh=not args.no_ssh_wait))
    except asyncio.CancelledError:
        print_line("\nInterrupted, shutting down...")
        rc = 130
    except KeyboardInterrupt:
        rc = 130
    return rc


if __name__ == "__main__":
    sys.exit(main())

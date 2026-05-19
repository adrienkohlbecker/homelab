#!/usr/bin/env -S uv run
"""Launch a QEMU machine via the test harness driver, no role/ansible.

Wraps machine.QemuMachine for interactive use: pick a variant, the harness
does image overlays + (on aarch64 ZFS) reads the packer-shipped
kernel/initrd next to the qcow2 + qemu launch. After boot it prints the
SSH command, leaves the VM up, and blocks until Ctrl-C.

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
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from machine import (
    DEFAULT_UBUNTU,
    QEMU_MACHINE_SPECS,
    QemuMachine,
    UBUNTU_RELEASES,
)
from utils import cancel_on_signal, print_cmd_line, print_line, tee_output


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
        help="Kernel cmdline (used with --kernel/--initrd). The harness " "auto-appends an arch-appropriate serial console= + earlycon= unless " "you already supplied one (ttyAMA on aarch64, ttyS on x86_64).",
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
        help="Mount PATH on the host as a 9p share with mount_tag=TAG inside " "the guest (`mount -t 9p TAG /mnt`). Repeatable.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Inherit qemu's stdio and use -serial mon:stdio so HMP is " "reachable via Ctrl-A,c (Ctrl-A,x to quit). The boot log is NOT " "captured to a file; implies --no-ssh-wait.",
    )
    parser.add_argument(
        "--qmp",
        type=Path,
        default=None,
        metavar="SOCKET",
        help="Bind qemu's QMP server to the given unix socket path.",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the packer artifact directory the harness reads "
        "(packer-ubuntu-1..N.{raw,qcow2} + efivars.fd) instead of the variant's "
        "default <imagedir>/<ubuntu>/<packer_image>. Lets qemu.pkr.hcl's "
        "verify-boot post-processor smoke-test a freshly-built `.new` "
        "directory before it's swapped over the previous good artifact.",
    )
    parser.add_argument(
        "--no-ssh-wait",
        action="store_true",
        help="Skip the SSH ready-check after boot -- e.g. when launching ZBM " "or any payload that doesn't expose sshd",
    )
    parser.add_argument(
        "--exit-after-ready",
        action="store_true",
        help="Exit cleanly after the SSH ready-check succeeds instead of " "blocking until Ctrl-C. Smoke-test mode: prove the image boots, " "then shut down. Mutually exclusive with --foreground and " "--no-ssh-wait (nothing to wait on for either).",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=None,
        metavar="PLAYBOOK",
        help="After SSH + system-running ready, run ansible-playbook " "PLAYBOOK against the booted VM, then cleanly shut down. Used by " "mise-tasks/packer/seed-deps to bake deps into a derived variant " "image. Implies --exit-after-ready semantics.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Skip the qcow2 overlay step for the OS disks and mount the " "--image-dir's packer-ubuntu-N.<format> files directly into qemu. " "Writes during the run mutate those files in place. Requires " "--image-dir to be explicit so it can't accidentally corrupt a " "published artifact directory.",
    )

    args = parser.parse_args()
    if (args.kernel is None) != (args.initrd is None):
        parser.error("--kernel and --initrd must be provided together")
    if args.exit_after_ready and (args.foreground or args.no_ssh_wait):
        parser.error("--exit-after-ready requires waiting for SSH; cannot combine with --foreground or --no-ssh-wait")
    if args.seed is not None and (args.foreground or args.no_ssh_wait):
        parser.error("--seed requires waiting for SSH; cannot combine with --foreground or --no-ssh-wait")
    if args.seed is not None and not args.seed.exists():
        parser.error(f"--seed playbook not found: {args.seed}")
    if args.commit and args.image_dir is None:
        parser.error("--commit requires --image-dir to be set explicitly (refusing to mutate the published artifact directory)")
    try:
        args.virtfs = _parse_virtfs(args.virtfs)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


async def _run_async(m: QemuMachine, *, wait_for_ssh: bool, exit_after_ready: bool, seed: Path | None) -> None:
    """Default flow: prepare + boot + ensure_ssh + wait, all under asyncio.

    With exit_after_ready, skip the m.wait() block — the async with unwinds
    after ensure_ssh succeeds and `systemctl is-system-running --wait` returns
    a clean state. Boot, SSH, or systemd-state failure surfaces as an
    exception (non-zero exit).

    With seed, after system-running passes, run ansible-playbook SEED
    against the booted VM and then trigger a clean poweroff over SSH.
    The async-with unwinds when qemu exits.
    """
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
                    if exit_after_ready or seed is not None:
                        raise
            if exit_after_ready or seed is not None:
                # SSH up != systemd "boot complete" — sshd can answer before
                # all units settle. Block on the system reaching a final
                # state; "running" is success, anything else (degraded,
                # maintenance) is a fail and worth surfacing.
                result = await m.ssh_command("systemctl", "is-system-running", "--wait", check=False)
                state = "\n".join(result.stdout).strip()
                if result.exitcode == 0 and state == "running":
                    print_line(f"System fully booted: {state}")
                else:
                    failed = await m.ssh_command("systemctl", "--failed", "--no-legend", check=False)
                    failed_units = "\n".join(failed.stdout).rstrip() or "(none)"
                    print_line(f"System reached state {state!r} (rc={result.exitcode}); failed units:\n" f"{failed_units}")
                    raise RuntimeError(f"systemd is-system-running returned {state!r}")
            if seed is not None:
                # Run the seed playbook against the booted VM, then ask
                # systemd to poweroff. The qemu process exits on guest
                # shutdown, which lets the async-with unwind normally —
                # same path as a Ctrl-C-driven shutdown.
                #
                # Stage the playbook into the workdir alongside the
                # roles/group_vars/host_vars that prepare() already
                # copied there. Ansible's group_vars/host_vars lookup
                # is relative to the playbook's dir; the original
                # packer/seed_deps.yml location has no group_vars
                # sibling, so `ubuntu_mirror` (and friends from
                # group_vars/all.yml) would otherwise be undefined.
                staged_seed = Path(m.workdir.name) / seed.name
                shutil.copy(seed, staged_seed)
                print_line(f"Seeding image via {seed}")
                await m.ansible_command(str(staged_seed))
                print_line("Seed playbook complete; powering off")
                # `&` so sshd doesn't hang on the connection while the
                # system tears down; check=False because the SSH channel
                # may close before ssh returns 0.
                await m.ssh_command("sudo", "systemctl", "poweroff", check=False)
                await m.wait()
            elif not exit_after_ready:
                await m.wait()


def _run_foreground(m: QemuMachine) -> int:
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

    m = QemuMachine(
        machine=args.machine,
        role="_launch",
        keep_vm=True,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        image_dir=args.image_dir,
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
        commit_in_place=args.commit,
    )

    if args.foreground:
        return _run_foreground(m)

    rc = 0
    try:
        with tee_output(m.output_file):
            asyncio.run(
                _run_async(
                    m,
                    wait_for_ssh=not args.no_ssh_wait,
                    exit_after_ready=args.exit_after_ready,
                    seed=args.seed,
                )
            )
    except asyncio.CancelledError:
        print_line("\nInterrupted, shutting down...")
        rc = 130
    except KeyboardInterrupt:
        rc = 130
    return rc


if __name__ == "__main__":
    sys.exit(main())

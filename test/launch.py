#!/usr/bin/env -S uv run
"""Launch a QEMU machine via the test harness driver, without Ansible.

Pick a variant and the harness prepares its image overlays and launches QEMU.
After boot it prints the SSH command, leaves the VM up, and blocks until
Ctrl-C; --exit-after-ready instead shuts down once systemd is running, which
is packer:build's verify boot. Pass --kernel/--append to direct-boot a unified
EFI image such as ZFSBootMenu's against the variant's disks:

  test/launch.py --machine lab --kernel /tmp/zbm/zfsbootmenu.EFI \\
      --append 'zbm.show earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200' \\
      --foreground
"""

import argparse
import contextlib
import signal
import subprocess
import sys
from pathlib import Path

from machine import QEMU_MACHINE_SPECS, LaunchOptions, Machine
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import print_cmd_line, print_line

# Bounds an --exit-after-ready verify end to end: boot, SSH, and systemd
# settling.
EXIT_AFTER_READY_TIMEOUT = 300


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
        "--kernel",
        type=Path,
        help="Unified EFI image (e.g. ZBM's zfsbootmenu.EFI) to direct-boot; it "
        "embeds its own initrd. Needs a Packer variant, which attaches UEFI.",
    )
    parser.add_argument(
        "--append",
        default="",
        help="Kernel cmdline (used with --kernel), passed verbatim. Include the "
        "arch's serial console= (ttyAMA0 on aarch64, ttyS0 on x86_64) to capture "
        "kernel output.",
    )
    parser.add_argument(
        "--mem",
        type=int,
        default=None,
        metavar="MIB",
        help="Guest RAM in MiB, overriding the machine spec.",
    )
    parser.add_argument(
        "--vcpus",
        type=int,
        default=None,
        metavar="N",
        help="Guest vCPU count, overriding the machine spec. Boots through "
        "ZFSBootMenu need 1 under stock HVF QEMU: a kexec'd kernel cannot bring its "
        "secondary CPUs online there (fixed by `mise run qemu:install_hvf_patched`).",
    )
    # Operator-only interactive mode; automated callers use the default harness path.
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Inherit qemu's stdio and use -serial mon:stdio so HMP is "
        "reachable via Ctrl-A,c (Ctrl-A,x to quit); its `info usernet` shows "
        "the SSH port. The boot log is NOT captured to a file, and nothing "
        "waits for SSH.",
    )
    parser.add_argument(
        "--display-window",
        action="store_true",
        help="Use qemu's local GUI display backend instead of VNC. Mainly "
        "useful with --foreground when testing boot UIs.",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the packer artifact directory the harness reads "
        "(packer-ubuntu-1..N.{raw,qcow2} + efivars.fd) instead of the variant's "
        "default <imagedir>/<ubuntu>/<machine>. Lets packer:build smoke-test a "
        "freshly-built staging directory before publishing it.",
    )
    parser.add_argument(
        "--exit-after-ready",
        action="store_true",
        help="Shut down once systemd reports running instead of blocking "
        "until Ctrl-C: prove the image boots, then exit. Headless, and "
        "incompatible with --foreground and --display-window.",
    )

    args = parser.parse_args()
    if args.exit_after_ready and (args.foreground or args.display_window):
        parser.error("--exit-after-ready runs headless; cannot combine with --foreground or --display-window")
    return args


def _dump_boot_console(m: Machine, lines: int = 200) -> None:
    """Print the tail of the captured serial console (the boot log).

    A boot that never reaches SSH is otherwise opaque -- this surfaces where it
    stalled (failed mount, emergency shell, a hung unit) right in the run
    output, so a verify-boot failure is diagnosable without re-running with
    --keep. Relies on the image booting with a serial console=, set on the ZBM
    cmdline in packer/scripts/chroot.sh.
    """
    try:
        captured = m.boot_file.read_text(errors="replace").splitlines()
    except OSError as exc:
        print_line(f"(boot console {m.boot_file} unavailable: {exc})")
        return
    tail = captured[-lines:]
    print_line(f"--- boot console tail ({len(tail)}/{len(captured)} lines) ---")
    for line in tail:
        print_line(line)
    print_line("--- end boot console ---")


def _run(m: Machine, *, exit_after_ready: bool) -> None:
    """Boot and wait for SSH under the harness session.

    A kept VM stays up afterwards until Ctrl-C (the session prints the SSH
    command and waits); --exit-after-ready instead checks systemd and shuts
    down. Boot, SSH, or systemd-state failure dumps the boot console and
    raises.
    """
    # Outside the session, so the overall deadline (a TimeoutError only once
    # the session exits) dumps the console too; the log outlives the VM.
    try:
        with m.session(EXIT_AFTER_READY_TIMEOUT if exit_after_ready else None):
            m.ensure_booted()
            print_line("Booted")
            m.ensure_ssh()
            print_line("SSH up")
            if exit_after_ready:
                m.ensure_system_running()
    except RuntimeError, TimeoutError:
        _dump_boot_console(m)
        raise


def _run_foreground(m: Machine) -> int:
    """Run qemu as if a shell exec'd it: inherited stdio, controlling tty
    intact, no intermediary readers competing for fd 0.

    Skips ensure_booted/ensure_ssh -- those are useful when the harness is
    driving an unattended boot, but in foreground the user *is* the
    monitor and the polling output (sleep_tick dots, status lines) would
    just clutter the qemu serial console.
    """
    try:
        m.prepare()
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
        m.stop()


def main() -> int:
    args = parse_args()

    m = Machine(
        machine=args.machine,
        role="_launch",
        keep_vm=not args.exit_after_ready,
        ubuntu_name=args.ubuntu,
        machine_timeout=EXIT_AFTER_READY_TIMEOUT if args.exit_after_ready else 0,
        launch=LaunchOptions(
            image_dir=args.image_dir,
            kernel=args.kernel,
            append=args.append,
            foreground=args.foreground,
            display_window=args.display_window,
        ),
        vcpus=args.vcpus,
        memory_mb=args.mem,
    )

    if args.foreground:
        return _run_foreground(m)

    return m.run(lambda: _run(m, exit_after_ready=args.exit_after_ready), "launch")


if __name__ == "__main__":
    sys.exit(main())

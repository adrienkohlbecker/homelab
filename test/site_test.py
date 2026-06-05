#!/usr/bin/env -S uv run
"""
Full site.yml converge on a box VM.

Boots a box QEMU VM, configures mirrors (apt/podman/pip via Nexus), then
runs the real site.yml with --limit box. Catches role-ordering and
cross-role interaction bugs that per-role tests miss.

Exit codes match testrole.py: 0 success, 1 converge failure, 124 timeout,
130 interrupted.
"""

import argparse
import asyncio
import contextlib
import shutil
import sys
import traceback
from pathlib import Path

from machine import (
    DEFAULT_UBUNTU,
    QemuMachine,
    UBUNTU_RELEASES,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from utils import CommandFailedException, cancel_on_signal, print_line, tee_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ubuntu",
        default=DEFAULT_UBUNTU,
        choices=sorted(UBUNTU_RELEASES),
        help="Ubuntu release codename (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=2400,
        metavar="SECONDS",
        help="Overall timeout for boot + converge (default: %(default)s)",
    )
    parser.add_argument(
        "--workdir-parent",
        type=Path,
        default=None,
        metavar="PATH",
        help="Parent directory for the per-run workdir (default: imagedir)",
    )
    return parser.parse_args()


async def run_site_test(m: QemuMachine, *, timeout: int) -> None:
    task = asyncio.current_task()
    assert task is not None

    with cancel_on_signal(task):
        async with asyncio.timeout(timeout):
            async with m:
                await m.ensure_booted()
                print_line("Booted")

                await m.ensure_ssh()
                print_line("SSH up")

                result = await m.ssh_command("systemctl", "is-system-running", "--wait", check=False)
                state = "\n".join(result.stdout).strip()
                if result.exitcode != 0 or state != "running":
                    failed = await m.ssh_command("systemctl", "--failed", "--no-legend", check=False)
                    failed_units = "\n".join(failed.stdout).rstrip() or "(none)"
                    print_line(f"System state {state!r}; failed units:\n{failed_units}")
                    raise RuntimeError(f"systemd is-system-running returned {state!r}")
                print_line(f"System ready: {state}")

                print_line("Running mirrors prelude")
                await m.ansible_command(f"{m.workdir.name}/_mirrors.yml")

                staged = Path(m.workdir.name) / "site.yml"
                shutil.copy(Path("site.yml"), staged)

                print_line("Running site.yml converge")
                try:
                    await m.ansible_command(str(staged))
                except CommandFailedException:
                    print_line("Site converge failed")
                    with contextlib.suppress(Exception):
                        await m.collect_failure_artifacts()
                    raise

                print_line("Site converge passed")
                await m.ssh_command("sudo", "systemctl", "poweroff", check=False)
                await m.wait()


def main() -> int:
    args = parse_args()

    sweep_stale_workdirs(imagedir_for_host())

    m = QemuMachine(
        machine="box",
        role="_site_test",
        keep_vm=False,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        workdir_parent=args.workdir_parent,
    )

    rc = 0
    with tee_output(m.output_file):
        try:
            asyncio.run(run_site_test(m, timeout=args.timeout))
        except CommandFailedException as exc:
            print_line(str(exc), error=True)
            print_line("site_test failed", error=True)
            rc = 1
        except TimeoutError:
            print_line(f"site_test timed out after {args.timeout}s", error=True)
            rc = 124
        except asyncio.CancelledError:
            print_line("\nInterrupted, shutting down...")
            rc = 130
        except Exception:
            print_line(traceback.format_exc().rstrip(), error=True)
            print_line("site_test crashed", error=True)
            rc = 1

    if rc == 0:
        m.cleanup_logs()

    return rc


if __name__ == "__main__":
    sys.exit(main())

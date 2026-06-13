#!/usr/bin/env -S uv run
"""
Full site.yml converge on a box fixture.

Boots a box fixture (qemu VM or, with --backend aws, a single-use EC2 cell),
configures mirrors (apt/podman/pip via Nexus, or upstream on aws), then
runs the real site.yml with --limit box. Catches role-ordering and
cross-role interaction bugs that per-role tests miss.

Exit codes match testrole.py: 0 success, 1 converge failure, 86 spot
interruption (EC2 backend), 124 timeout, 130 interrupted.
"""

import argparse
import asyncio
import contextlib
import os
import shutil
import sys
import traceback
from pathlib import Path

from machine import (
    BACKEND_CHOICES,
    DEFAULT_UBUNTU,
    Machine,
    UBUNTU_RELEASES,
    create_machine,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from utils import (
    CommandFailedException,
    SpotInterruptedException,
    cancel_on_signal,
    print_line,
    tee_output,
)


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
        "--backend",
        default=os.environ.get("HOMELAB_TEST_BACKEND", "qemu"),
        choices=BACKEND_CHOICES,
        help="Where the fixture runs: a local qemu VM or a single-use EC2 spot instance "
        "(notes/ci_aws_test_cells.md). Falls back to $HOMELAB_TEST_BACKEND, then qemu.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=2400,
        metavar="SECONDS",
        help="Overall timeout for boot + converge (default: %(default)s)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test (for debugging)",
    )
    parser.add_argument(
        "--workdir-parent",
        type=Path,
        default=None,
        metavar="PATH",
        help="Parent directory for the per-run workdir (default: imagedir)",
    )
    return parser.parse_args()


async def run_site_test(m: Machine, *, timeout: int) -> None:
    task = asyncio.current_task()
    assert task is not None

    # When --keep is set and the deadline fires, absorb the cancel so the
    # VM stays up for debugging. Re-surface TimeoutError after the wait.
    timer_absorbed = False

    with cancel_on_signal(task):
        async with asyncio.timeout(timeout) as timeout_cm:
            async with m:
                try:
                    try:
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
                            # Post-hoc spot classification (EC2 backend only;
                            # the base returns False). A reclaimed instance is
                            # infrastructure noise, not a converge failure —
                            # exit 86 so the GitLab job retries exactly once.
                            spot_interrupted = False
                            with contextlib.suppress(Exception):
                                spot_interrupted = await m.failure_was_spot_interruption()
                            if spot_interrupted:
                                raise SpotInterruptedException(
                                    "Site converge failure classified as a spot interruption"
                                ) from None
                            raise

                        print_line("Site converge passed")
                        if not m.keep_vm:
                            await m.ssh_command("sudo", "systemctl", "poweroff", check=False)
                            await m.wait()

                    except asyncio.CancelledError:
                        if m.keep_vm and timeout_cm.expired() and task.cancelling():
                            task.uncancel()
                            timer_absorbed = True
                            print_line(
                                f"Timed out after {timeout}s; --keep set, dropping to SSH for debug",
                            )
                        else:
                            raise

                finally:
                    if m.keep_vm and not task.cancelling():
                        with contextlib.suppress(RuntimeError):
                            timeout_cm.reschedule(None)
                        m.print_ssh_instructions()
                        await m.wait()

    if timer_absorbed:
        raise TimeoutError(f"site_test timed out after {timeout}s")


def main() -> int:
    args = parse_args()

    # qemu-only: the aws backend stages its workdir under the system tmp and
    # the imagedir mount may not exist at all on the runner container
    # (see testrole.py's matching guard).
    if args.backend == "qemu":
        sweep_stale_workdirs(imagedir_for_host())

    m: Machine = create_machine(
        args.backend,
        machine="box",
        role="_site_test",
        keep_vm=args.keep,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        workdir_parent=args.workdir_parent,
    )

    rc = 0
    with tee_output(m.output_file):
        try:
            asyncio.run(run_site_test(m, timeout=args.timeout))
        except SpotInterruptedException as exc:
            print_line(str(exc), error=True)
            print_line("site_test spot-interrupted", error=True)
            rc = 86  # reserved for spot interruption; GitLab jobs retry this once
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

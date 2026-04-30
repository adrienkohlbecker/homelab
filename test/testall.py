#!/usr/bin/env -S uv run
"""
Test runner for Ansible roles using native asyncio parallelism.

This script discovers roles, builds test commands, and executes them concurrently
without relying on GNU parallel. Each child testrole.py tees its own transcript
to `test/out/<machine>.<ubuntu>.<role>.output.ansi` and, when invoked with
--no-keep-logs (as testall does), drops its output/boot/journal logs on a
clean pass; failed runs leave them in place under that predictable path. A
concise job log is written to `test/out.tsv`.
"""

import argparse
import asyncio
import contextlib
import csv
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from tabulate import tabulate

from build_image import build_image
from machine import DEFAULT_UBUNTU, OUT_DIR, UBUNTU_RELEASES, ensure_podman_network
from utils import STREAM_COLORS, cancel_on_signal

LOG_FILE = Path("test/out.tsv")
LOG_FILE_PREV = Path("test/out.tsv.prev")
JOBLOG_FIELDS = ["Role", "Ubuntu", "Machine", "Runtime", "Exitval", "Started"]


class MachineRole(NamedTuple):
    """A (machine, ubuntu, role) triple to run."""
    machine: str
    ubuntu_name: str
    role: str


@dataclass(frozen=True)
class JobResult:
    """Holds the outcome of a single role test."""

    machine: str
    ubuntu_name: str
    role: str
    runtime: float
    exitval: int
    started_at: str


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for selecting machines, roles, and concurrency."""
    parser = argparse.ArgumentParser(
        description="Run Ansible role tests concurrently",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Rerun only roles that failed in the last log",
    )

    parser.add_argument(
        "--jobs",
        type=int,
        default=5,
        metavar="N",
        help="Number of parallel workers (default: 5)",
    )

    parser.add_argument(
        "--machines",
        type=str,
        default="container",
        metavar="X",
        help="Comma-separated list of machine profiles (default: container)",
    )

    parser.add_argument(
        "--ubuntu",
        type=str,
        default=DEFAULT_UBUNTU,
        metavar="X",
        help=f"Comma-separated list of Ubuntu codenames (default: {DEFAULT_UBUNTU})",
    )

    parser.add_argument(
        "--checkmode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run ansible in check mode before each test (default: on; --no-checkmode disables)",
    )

    parser.add_argument(
        "--idempotence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Re-run each role and fail if any task reports changed (default: on)",
    )

    parser.add_argument(
        "--build-image",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rebuild the homelab:<codename> container image(s) before running (default: on)",
    )

    # Remaining arguments are forwarded to testrole.py
    parser.add_argument(
        "role_args",
        nargs="*",
        help="Additional arguments to forward to testrole.py",
    )

    return parser.parse_args()


def setup_output_dir() -> None:
    """Create the output directory and remove stale .ansi logs."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for ansi_file in OUT_DIR.glob("*.ansi"):
        ansi_file.unlink()


def list_roles() -> list[str]:
    """Return roles that define tasks/main.yml."""
    roles_dir = Path("roles")
    if not roles_dir.exists():
        return []

    return [
        role_dir.name
        for role_dir in sorted(roles_dir.iterdir())
        if role_dir.is_dir() and (role_dir / "tasks" / "main.yml").exists()
    ]


def get_failed_roles() -> list[MachineRole]:
    """Parse the previous job log for failed roles."""
    if not LOG_FILE.exists():
        return []

    failed_roles: list[MachineRole] = []

    with LOG_FILE.open(encoding="utf-8", newline="") as log_file:
        for row in csv.DictReader(log_file, delimiter="\t"):
            if row["Exitval"] != "0":
                failed_roles.append(
                    MachineRole(
                        machine=row["Machine"],
                        ubuntu_name=row["Ubuntu"],
                        role=row["Role"],
                    )
                )

    return failed_roles


def _rotate_joblog() -> None:
    """Rotate the job log, preserving the previous run for inspection."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists():
        if LOG_FILE_PREV.exists():
            LOG_FILE_PREV.unlink()
        LOG_FILE.rename(LOG_FILE_PREV)


async def _run_role(
    seq: int,
    machine: str,
    ubuntu_name: str,
    role: str,
    role_args: Sequence[str],
    semaphore: asyncio.Semaphore,
    checkmode: bool,
    idempotence: bool,
) -> JobResult:
    """Execute a single role test while respecting the concurrency limit."""
    cmd = [
        "test/testrole.py",
        "--machine", machine,
        "--ubuntu", ubuntu_name,
        "--checkmode" if checkmode else "--no-checkmode",
        "--idempotence" if idempotence else "--no-idempotence",
        # testall builds images upfront; tell each child to skip the rebuild.
        "--no-build-image",
        # Let testrole drop its own logs on a clean pass so we don't leak
        # noise files into test/out/ for green roles.
        "--no-keep-logs",
        role,
        *role_args,
    ]
    # testrole.py tees its full transcript via utils.tee_output and owns the
    # file's lifecycle (drops it on success, keeps it on failure under a
    # known path), so testall doesn't need to track or surface log paths.

    async with semaphore:
        start_time = time.time()
        started_at = datetime.fromtimestamp(start_time, tz=UTC).isoformat(timespec="seconds")
        print(f"[{seq}] {machine}:{ubuntu_name}:{role} starting")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await proc.wait()
        except BaseException:
            # Any failure (cancellation, reader error, etc.) leaves the
            # subprocess behind unless we tear it down here.
            with contextlib.suppress(ProcessLookupError):
                # Race: the testrole child may have exited on its own
                # between the wait and our signal. SIGINT mirrors what
                # Machine.stop() sends to its qemu/podman child, so the
                # whole chain reacts to the same signal.
                proc.send_signal(signal.SIGINT)
            try:
                # 30s gives Machine.stop() enough headroom for its own
                # graceful->SIGKILL escalation (~10-15s worst case for
                # podman rm --time 5 + drain, similar for qemu) without
                # SIGKILL'ing testrole.py mid-cleanup and leaking the
                # container/VM.
                async with asyncio.timeout(30):
                    await proc.wait()
            except TimeoutError:
                # asyncio.timeout() converts the inner CancelledError into
                # TimeoutError on __aexit__; only here can we tell the wait
                # actually timed out and escalate to SIGKILL.
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            raise

        runtime = time.time() - start_time
        # proc.returncode is always set after a successful proc.wait(); a
        # negative value means killed-by-signal N -- normalize to shell's
        # 128+N convention.
        exitval = proc.returncode
        assert exitval is not None
        if exitval < 0:
            exitval = 128 - exitval
        status = "ok" if exitval == 0 else STREAM_COLORS["stderr"].format(line="fail")
        # Per-run log cleanup is testrole's responsibility now (--no-keep-logs above).
        print(f"[{seq}] {machine}:{ubuntu_name}:{role} {status} ({runtime:.1f}s)")

    return JobResult(
        machine=machine,
        ubuntu_name=ubuntu_name,
        role=role,
        runtime=runtime,
        exitval=exitval,
        started_at=started_at,
    )


async def run_all(
    machine_roles: list[MachineRole],
    role_args: Sequence[str],
    jobs: int,
    checkmode: bool,
    idempotence: bool,
) -> list[JobResult]:
    """Run every role/machine combination concurrently."""
    semaphore = asyncio.Semaphore(jobs)

    task = asyncio.current_task()
    assert task is not None

    # Provision shared podman state once up front. Doing it here (instead of
    # letting each child testrole.py race in PodmanMachine.prepare()) means
    # the per-worker inspect calls find the network already present and skip
    # the racy create.
    if any(mr.machine == "container" for mr in machine_roles):
        await ensure_podman_network()

    with cancel_on_signal(task):
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    _run_role(seq, mr.machine, mr.ubuntu_name, mr.role, role_args, semaphore, checkmode, idempotence)
                )
                for seq, mr in enumerate(machine_roles, start=1)
            ]

    return [t.result() for t in tasks]


def _print_failure_table(failures: list[JobResult]) -> None:
    """Render a table of failed runs."""
    rows = [
        [result.machine, result.ubuntu_name, result.role]
        for result in sorted(failures, key=lambda r: (r.machine, r.ubuntu_name, r.role))
    ]
    print("\nFailure summary:", file=sys.stderr)
    print(tabulate(rows, headers=["Machine", "Ubuntu", "Role"]), file=sys.stderr)


def _write_joblog(results: list[JobResult]) -> None:
    """Write a compact job log with role, ubuntu, machine, runtime, exit code, and start time."""
    with LOG_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOBLOG_FIELDS, delimiter="\t")
        writer.writeheader()
        for result in results:
            writer.writerow({
                "Role": result.role,
                "Ubuntu": result.ubuntu_name,
                "Machine": result.machine,
                "Runtime": f"{result.runtime:.3f}",
                "Exitval": result.exitval,
                "Started": result.started_at,
            })


def main() -> int:
    """Entry point for running tests."""
    args = parse_args()

    if args.jobs < 1:
        print("Error: --jobs must be at least 1", file=sys.stderr)
        return 1

    if args.only_failed:
        machine_roles = get_failed_roles()
        if not machine_roles:
            print(f"No failed roles recorded in {LOG_FILE}", file=sys.stderr)
            return 0
    else:
        machines = [m.strip() for m in args.machines.split(",") if m.strip()]
        if not machines:
            print("Error: No machines provided to --machines", file=sys.stderr)
            return 1

        ubuntus = [u.strip() for u in args.ubuntu.split(",") if u.strip()]
        if not ubuntus:
            print("Error: No codenames provided to --ubuntu", file=sys.stderr)
            return 1
        for u in ubuntus:
            if u not in UBUNTU_RELEASES:
                print(
                    f"Error: unknown Ubuntu codename '{u}'; valid: {sorted(UBUNTU_RELEASES)}",
                    file=sys.stderr,
                )
                return 1

        roles = list_roles()
        if not roles:
            print("No roles with tasks/main.yml found", file=sys.stderr)
            return 1
        machine_roles = [
            MachineRole(machine, ubuntu_name, role)
            for role in roles
            for ubuntu_name in ubuntus
            for machine in machines
        ]

    setup_output_dir()

    if args.build_image:
        needs_image = any(mr.machine == "container" for mr in machine_roles)
        if needs_image:
            for codename in dict.fromkeys(mr.ubuntu_name for mr in machine_roles):
                rc = build_image(codename)
                if rc != 0:
                    print(f"Image build failed for homelab:{codename}", file=sys.stderr)
                    return rc

    try:
        results = asyncio.run(
            run_all(machine_roles, args.role_args, args.jobs, args.checkmode, args.idempotence)
        )
    except asyncio.CancelledError:
        print("\nInterrupted, shutting down...", file=sys.stderr)
        return 130

    _rotate_joblog()
    _write_joblog(results)

    failures = [result for result in results if result.exitval != 0]
    if failures:
        _print_failure_table(failures)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

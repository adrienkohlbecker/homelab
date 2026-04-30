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
from machine import (
    DEFAULT_UBUNTU,
    MACHINE_CHOICES,
    OUT_DIR,
    UBUNTU_RELEASES,
    ensure_podman_network,
)
from utils import cancel_on_signal, colorize, terminate_subprocess

LOG_FILE = Path("test/out.tsv")
LOG_FILE_PREV = Path("test/out.tsv.prev")
JOBLOG_FIELDS = ["Role", "Ubuntu", "Machine", "Runtime", "Exitval", "Started"]
LIVENESS_TICK_SECONDS = 300.0  # 5 minutes

# Flags testall.py controls per child invocation. If a user passes any of
# them through role_args, testrole.py's argparse last-wins would silently
# flip the meaning -- reject up front so the conflict is obvious.
TESTROLE_OWNED_FLAGS = frozenset({
    "--machine",
    "--ubuntu",
    "--checkmode", "--no-checkmode",
    "--idempotence", "--no-idempotence",
    "--build-image", "--no-build-image",
    "--keep-logs", "--no-keep-logs",
    "--keep",
})


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

    parser.add_argument(
        "--role",
        type=str,
        default="",
        metavar="X",
        help="Comma-separated list of roles to run (default: all roles with tasks/main.yml)",
    )

    parser.add_argument(
        "--exclude",
        type=str,
        default="",
        metavar="X",
        help="Comma-separated list of roles to exclude",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved (machine, ubuntu, role) plan and exit",
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


async def _emit_liveness(seq: int, machine: str, ubuntu_name: str, role: str, start_time: float) -> None:
    """Print a periodic 'still running' message until cancelled."""
    while True:
        await asyncio.sleep(LIVENESS_TICK_SECONDS)
        elapsed_min = (time.time() - start_time) / 60.0
        print(f"[{seq}] {machine}:{ubuntu_name}:{role} still running, {elapsed_min:.0f}m elapsed")


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
        liveness = asyncio.create_task(_emit_liveness(seq, machine, ubuntu_name, role, start_time))

        try:
            try:
                await proc.wait()
            except BaseException:
                # 30s gives Machine.stop() enough headroom for its own
                # graceful->SIGKILL escalation (~10-15s worst case for
                # podman rm --time 5 + drain, similar for qemu) without
                # SIGKILL'ing testrole.py mid-cleanup and leaking the
                # container/VM. SIGINT mirrors what Machine.stop() sends to
                # its qemu/podman child so the whole chain reacts the same.
                await terminate_subprocess(proc, grace_seconds=30, initial_signal=signal.SIGINT)
                raise
        finally:
            liveness.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await liveness

        runtime = time.time() - start_time
        # proc.returncode is always set after a successful proc.wait(); a
        # negative value means killed-by-signal N -- normalize to shell's
        # 128+N convention.
        exitval = proc.returncode
        assert exitval is not None
        if exitval < 0:
            exitval = 128 - exitval
        status = "ok" if exitval == 0 else colorize("fail", "red")
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
) -> tuple[list[JobResult], bool]:
    """Run every role/machine combination concurrently.

    Returns (results, cancelled). On cancellation, results contains only the
    jobs that finished before the cancel cascade fired so the caller can still
    persist a partial joblog.
    """
    semaphore = asyncio.Semaphore(jobs)

    task = asyncio.current_task()
    assert task is not None

    tasks: list[asyncio.Task[JobResult]] = []
    cancelled = False
    try:
        with cancel_on_signal(task):
            # Provision shared podman state once up front. Doing it here
            # (instead of letting each child testrole.py race in
            # PodmanMachine.prepare()) means the per-worker inspect calls find
            # the network already present and skip the racy create. Inside
            # cancel_on_signal so a Ctrl+C during the network setup is caught
            # alongside cancellation during the actual run.
            if any(mr.machine == "container" for mr in machine_roles):
                await ensure_podman_network()
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        _run_role(seq, mr.machine, mr.ubuntu_name, mr.role, role_args, semaphore, checkmode, idempotence)
                    )
                    for seq, mr in enumerate(machine_roles, start=1)
                ]
    except asyncio.CancelledError:
        # When the parent task is cancelled, TaskGroup cascades cancellation
        # into every child and re-raises bare CancelledError on exit. Swallow
        # it here so the caller can see the partial results below.
        cancelled = True

    # Build a result for every (machine, ubuntu, role) so a follow-up
    # `testall.py --only-failed` retries anything that didn't pass -- whether
    # it ran to completion, was cancelled mid-run, or never got the chance
    # to start (cancel hit before / during TaskGroup setup).
    results: list[JobResult] = []
    for mr, t in zip(machine_roles, tasks, strict=False):
        if t.done() and not t.cancelled() and t.exception() is None:
            results.append(t.result())
        else:
            results.append(_cancelled_result(mr))
    for mr in machine_roles[len(tasks):]:
        results.append(_cancelled_result(mr))
    return results, cancelled


def _cancelled_result(mr: MachineRole) -> JobResult:
    """Synthetic JobResult for a job interrupted before it could record its own."""
    return JobResult(
        machine=mr.machine,
        ubuntu_name=mr.ubuntu_name,
        role=mr.role,
        runtime=0.0,
        # 130 = 128 + SIGINT, matching what testrole.py emits when it gets
        # cancelled itself, and what main() returns from this script.
        exitval=130,
        started_at="",
    )


def _print_failure_table(failures: list[JobResult]) -> None:
    """Render a table of failed runs with exit codes and runtime."""
    rows = [
        [r.machine, r.ubuntu_name, r.role, r.exitval, f"{r.runtime:.1f}s"]
        for r in sorted(failures, key=lambda r: (r.machine, r.ubuntu_name, r.role))
    ]
    print("\nFailure summary:", file=sys.stderr)
    print(
        tabulate(rows, headers=["Machine", "Ubuntu", "Role", "Exit", "Runtime"]),
        file=sys.stderr,
    )


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

    conflicts = [a for a in args.role_args if a in TESTROLE_OWNED_FLAGS]
    if conflicts:
        print(
            f"Error: testall.py controls these flags itself; pass them to testall instead "
            f"of forwarding via role_args: {conflicts}",
            file=sys.stderr,
        )
        return 1

    if args.only_failed:
        if args.machines != "container" or args.ubuntu != DEFAULT_UBUNTU:
            print(
                "Warning: --machines/--ubuntu are ignored with --only-failed; "
                "the machine and ubuntu of each rerun come from the prior joblog",
                file=sys.stderr,
            )
        machine_roles = get_failed_roles()
        if not machine_roles:
            print(f"No failed roles recorded in {LOG_FILE}", file=sys.stderr)
            return 0
    else:
        machines = [m.strip() for m in args.machines.split(",") if m.strip()]
        if not machines:
            print("Error: No machines provided to --machines", file=sys.stderr)
            return 1
        for m in machines:
            if m not in MACHINE_CHOICES:
                print(
                    f"Error: unknown machine profile '{m}'; valid: {list(MACHINE_CHOICES)}",
                    file=sys.stderr,
                )
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

    if args.role:
        wanted = {r.strip() for r in args.role.split(",") if r.strip()}
        machine_roles = [mr for mr in machine_roles if mr.role in wanted]
    if args.exclude:
        excluded = {r.strip() for r in args.exclude.split(",") if r.strip()}
        machine_roles = [mr for mr in machine_roles if mr.role not in excluded]
    if not machine_roles:
        print("No roles match --role/--exclude filters", file=sys.stderr)
        return 0

    if args.list:
        for mr in machine_roles:
            print(f"{mr.machine}\t{mr.ubuntu_name}\t{mr.role}")
        return 0

    setup_output_dir()

    if args.build_image:
        needs_image = any(mr.machine == "container" for mr in machine_roles)
        if needs_image:
            try:
                for codename in dict.fromkeys(mr.ubuntu_name for mr in machine_roles):
                    rc = build_image(codename)
                    if rc != 0:
                        print(f"Image build failed for homelab:{codename}", file=sys.stderr)
                        return rc
            except KeyboardInterrupt:
                # build_image() runs its own asyncio.run(); SIGINT during a
                # build raises KeyboardInterrupt out of it. Translate that
                # into the same exit code as a cancelled run rather than
                # surfacing a stack trace.
                print("\nInterrupted during image build, shutting down...", file=sys.stderr)
                return 130

    test_start = time.time()
    results, cancelled = asyncio.run(
        run_all(machine_roles, args.role_args, args.jobs, args.checkmode, args.idempotence)
    )
    wall_clock = time.time() - test_start

    if results:
        _rotate_joblog()
        _write_joblog(results)

    if cancelled:
        # Synthesized cancellation entries have empty started_at; everything
        # else actually ran (whether it passed or failed).
        completed = sum(1 for r in results if r.started_at)
        msg = (
            f"\nInterrupted, shutting down ({completed}/{len(machine_roles)} completed); "
            f"joblog written to {LOG_FILE} -- rerun with --only-failed to retry the rest"
        )
        print(msg, file=sys.stderr)
        return 130

    failures = [result for result in results if result.exitval != 0]
    if failures:
        _print_failure_table(failures)
        return 1

    if results:
        longest = max(results, key=lambda r: r.runtime)
        print(
            f"\n{len(results)} role(s) passed in {wall_clock:.0f}s wall clock "
            f"(parallelism={args.jobs}, longest: {longest.role} on "
            f"{longest.machine}:{longest.ubuntu_name} at {longest.runtime:.0f}s)",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env -S uv run
"""
Test runner for Ansible roles using native asyncio parallelism.

This script discovers roles, builds test commands, and executes them concurrently
without relying on GNU parallel. Output for each machine/role run is captured in
`test/out/<machine>.<role>.ansi`, and a concise job log is written to
`test/out.log`.
"""

import argparse
import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

from machine import OUT_DIR

LOG_FILE = Path("test/out.log")
LOG_FILE_PREV = Path("test/out.log.prev")


@dataclass
class JobResult:
    """Holds the outcome of a single role test."""

    machine: str
    role: str
    runtime: float
    exitval: int


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


def list_roles() -> List[str]:
    """Return roles that define tasks/main.yml."""
    roles_dir = Path("roles")
    if not roles_dir.exists():
        return []

    return [
        role_dir.name
        for role_dir in sorted(roles_dir.iterdir())
        if role_dir.is_dir() and (role_dir / "tasks" / "main.yml").exists()
    ]


def get_failed_roles() -> List[List[str]]:
    """
    Parse the previous job log for failed roles.
    """
    if not LOG_FILE.exists():
        return []

    failed_roles: List[List[str]] = []

    with LOG_FILE.open(encoding="utf-8") as log_file:
        for line_no, raw_line in enumerate(log_file):
            if line_no == 0:
                continue  # header

            fields = raw_line.rstrip("\n").split("\t")
            if len(fields) < 4:
                continue

            role, machine, _runtime, exitval = fields[:4]
            if exitval != "0":
                failed_roles.append([machine, role])

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
    role: str,
    role_args: Sequence[str],
    semaphore: asyncio.Semaphore,
) -> JobResult:
    """Execute a single role test while respecting the concurrency limit."""
    cmd = ["test/testrole.py", "--machine", machine, role, "--checkmode", *role_args]
    log_path = OUT_DIR / f"{machine}.{role}.ansi"

    async with semaphore:
        start_time = time.time()
        print(f"[{seq}] {machine}:{role} starting")
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("w") as log_handle:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_handle,
                stderr=sys.stderr,
            )

            try:
                await proc.wait()
            except asyncio.CancelledError:
                proc.terminate()
                try:
                    async with asyncio.timeout(10):
                        await proc.wait()
                except TimeoutError:
                    # asyncio.timeout() converts the inner CancelledError into
                    # TimeoutError on __aexit__; only here can we tell the wait
                    # actually timed out and escalate to SIGKILL.
                    proc.kill()
                    await proc.wait()
                raise

        runtime = time.time() - start_time
        exitval = proc.returncode if proc.returncode is not None else 0
        if exitval < 0:
            exitval = 128 + (-exitval)
        status = "ok" if exitval == 0 else "fail"
        print(f"[{seq}] {machine}:{role} {status} ({runtime:.1f}s)")

    return JobResult(
        machine=machine,
        role=role,
        runtime=runtime,
        exitval=exitval,
    )


async def run_all(
    machine_roles: List[List[str]],
    role_args: Sequence[str],
    jobs: int,
) -> List[JobResult]:
    """Run every role/machine combination concurrently."""
    semaphore = asyncio.Semaphore(jobs)
    results: List[JobResult] = []

    loop = asyncio.get_running_loop()
    current = asyncio.current_task(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        if not current:
            raise RuntimeError("No current task")
        loop.add_signal_handler(sig, current.cancel)

    async def run_and_store(seq: int, machine: str, role: str) -> None:
        result = await _run_role(seq, machine, role, role_args, semaphore)
        results.append(result)

    try:
        async with asyncio.TaskGroup() as tg:
            seq = 1
            for machine_role in machine_roles:
                tg.create_task(run_and_store(seq, machine_role[0], machine_role[1]))
                seq += 1
    except asyncio.CancelledError:
        # TaskGroup already cancelled children; propagate the interrupt
        raise

    return results


def _write_joblog(results: List[JobResult]) -> None:
    """Write a compact job log with role, machine, runtime, and exit code."""
    with LOG_FILE.open("w", encoding="utf-8") as handle:
        handle.write("Role\tMachine\tRuntime\tExitval\n")

        for result in results:
            handle.write(f"{result.role}\t{result.machine}\t{result.runtime:.3f}\t{result.exitval}\n")


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

        roles = list_roles()
        if not roles:
            print("No roles with tasks/main.yml found", file=sys.stderr)
            return 1
        machine_roles = [[machine, role] for role in roles for machine in machines]

    setup_output_dir()

    try:
        results = asyncio.run(run_all(machine_roles, args.role_args, args.jobs))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nInterrupted, cancelling remaining jobs...", file=sys.stderr)
        return 130

    _rotate_joblog()
    _write_joblog(results)

    failures = [result for result in results if result.exitval != 0]
    if failures:
        failed_list = ", ".join({f.role for f in failures})
        print(f"Failures: {failed_list}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

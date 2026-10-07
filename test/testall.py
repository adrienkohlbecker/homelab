#!/usr/bin/env -S uv run
"""
Run every role-test cell from the meta/test.yml matrix in parallel.

Each child testrole.py tees its own transcript to
`test/out/<machine>.<ubuntu>.<role>.output.ansi`, drops per-run artifacts on a
clean pass, and keeps them for failures. A concise job log is written to
`test/out.tsv`; --retry-failed reruns only its failed cells and updates them in
place, while a full run replaces it.
"""

import argparse
import asyncio
import contextlib
import csv
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from matrix import TestCell, build_test_matrix, list_testable_roles
from utils import cancel_on_signal, colorize, terminate_subprocess

LOG_FILE = Path("test/out.tsv")
JOBLOG_FIELDS = ["Role", "Ubuntu", "Machine", "Runtime", "Exitval", "Started"]
LIVENESS_TICK_SECONDS = 300.0  # 5 minutes


@dataclass(frozen=True)
class JobResult:
    """Holds the outcome of a single role test."""

    cell: TestCell
    runtime: float
    exitval: int
    started_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Rerun only the cells that failed in test/out.tsv",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=5,
        metavar="N",
        help="Number of parallel workers (default: 5)",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    return args


def _read_joblog() -> list[JobResult]:
    """Load the current joblog, returning an empty list when it does not exist."""
    if not LOG_FILE.exists():
        return []

    with LOG_FILE.open(encoding="utf-8", newline="") as handle:
        return [
            JobResult(
                cell=TestCell(machine=row["Machine"], ubuntu=row["Ubuntu"], role=row["Role"]),
                runtime=float(row["Runtime"]),
                exitval=int(row["Exitval"]),
                started_at=row["Started"],
            )
            for row in csv.DictReader(handle, delimiter="\t")
        ]


def _write_joblog(results: list[JobResult]) -> None:
    """Write a compact job log with role, ubuntu, machine, runtime, exit code, and start time."""
    with LOG_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOBLOG_FIELDS, delimiter="\t")
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "Role": result.cell.role,
                    "Ubuntu": result.cell.ubuntu,
                    "Machine": result.cell.machine,
                    "Runtime": f"{result.runtime:.3f}",
                    "Exitval": result.exitval,
                    "Started": result.started_at,
                }
            )


async def _emit_liveness(seq: int, cell: TestCell, start_time: float) -> None:
    """Print a periodic 'still running' message until cancelled."""
    while True:
        await asyncio.sleep(LIVENESS_TICK_SECONDS)
        elapsed_min = (time.time() - start_time) / 60.0
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} still running, {elapsed_min:.0f}m elapsed")


async def _run_role(seq: int, cell: TestCell, semaphore: asyncio.Semaphore) -> JobResult:
    """Execute a single role test while respecting the concurrency limit."""
    async with semaphore:
        start_time = time.time()
        started_at = datetime.fromtimestamp(start_time, tz=UTC).isoformat(timespec="seconds")
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} starting")

        # testrole.py tees its full transcript into a per-run log and owns that
        # file's lifecycle, so its output stays detached here. A private stdin
        # keeps parallel children off the terminal.
        proc = await asyncio.create_subprocess_exec(
            "test/testrole.py",
            "--machine",
            cell.machine,
            "--ubuntu",
            cell.ubuntu,
            cell.role,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        liveness = asyncio.create_task(_emit_liveness(seq, cell, start_time))

        try:
            try:
                await proc.wait()
            except BaseException:
                # SIGINT with a 30s grace lets testrole.py's Machine.stop()
                # finish its own graceful->SIGKILL escalation of qemu
                # (~10-15s worst case) instead of leaking the VM.
                await terminate_subprocess(proc, grace_seconds=30)
                raise
        finally:
            liveness.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await liveness

        runtime = time.time() - start_time
        # A negative returncode means killed-by-signal N; normalize to the
        # shell's 128+N convention.
        exitval = proc.returncode
        assert exitval is not None
        if exitval < 0:
            exitval = 128 - exitval
        status = "ok" if exitval == 0 else colorize("fail", "red")
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} {status} ({runtime:.1f}s)")

    return JobResult(cell=cell, runtime=runtime, exitval=exitval, started_at=started_at)


def _cancelled_result(cell: TestCell) -> JobResult:
    """Synthetic JobResult for a job interrupted before it could record its own.

    130 = 128 + SIGINT, matching what testrole.py emits when it gets cancelled
    itself, and what main() returns from this script.
    """
    return JobResult(cell=cell, runtime=0.0, exitval=130, started_at="")


async def run_all(cells: list[TestCell], jobs: int) -> tuple[list[JobResult], bool]:
    """Run every cell concurrently; return (results, cancelled).

    On cancellation every cell that did not finish is recorded as cancelled, so
    a follow-up --retry-failed retries it whether it was interrupted mid-run or
    never started.
    """
    semaphore = asyncio.Semaphore(jobs)
    task = asyncio.current_task()
    assert task is not None

    tasks: list[asyncio.Task[JobResult]] = []
    cancelled = False
    try:
        with cancel_on_signal(task):
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_run_role(seq, cell, semaphore)) for seq, cell in enumerate(cells, start=1)]
    except asyncio.CancelledError:
        # TaskGroup cascades a parent cancellation into every child and
        # re-raises bare CancelledError; the partial results below still count.
        cancelled = True

    results = [
        t.result() if t.done() and not t.cancelled() and t.exception() is None else _cancelled_result(cell)
        for cell, t in zip(cells, tasks, strict=False)
    ]
    results.extend(_cancelled_result(cell) for cell in cells[len(tasks) :])
    return results, cancelled


def main() -> int:
    args = parse_args()

    prior = _read_joblog() if args.retry_failed else []
    if args.retry_failed:
        cells = [result.cell for result in prior if result.exitval != 0]
        if not cells:
            print(f"No failed roles recorded in {LOG_FILE}", file=sys.stderr)
            return 0
    else:
        cells = build_test_matrix(list_testable_roles())

    test_start = time.time()
    results, cancelled = asyncio.run(run_all(cells, args.jobs))
    wall_clock = time.time() - test_start

    merged = {result.cell: result for result in prior} | {result.cell: result for result in results}
    _write_joblog(list(merged.values()))

    if cancelled:
        # Synthesized cancellation entries have an empty started_at.
        completed = sum(1 for r in results if r.started_at)
        print(
            f"\nInterrupted, shutting down ({completed}/{len(cells)} completed); "
            f"joblog written to {LOG_FILE} -- rerun with --retry-failed to retry the rest",
            file=sys.stderr,
        )
        return 130

    failures = sorted((r for r in results if r.exitval != 0), key=lambda r: r.cell)
    if failures:
        print("\nFailure summary:", file=sys.stderr)
        for r in failures:
            print(
                f"  {r.cell.machine}:{r.cell.ubuntu}:{r.cell.role}  exit {r.exitval}  {r.runtime:.1f}s", file=sys.stderr
            )
        return 1

    longest = max(results, key=lambda r: r.runtime)
    print(
        f"\n{len(results)} role(s) passed in {wall_clock:.0f}s wall clock "
        f"(parallelism={args.jobs}, longest: {longest.cell.role} on "
        f"{longest.cell.machine}:{longest.cell.ubuntu} at {longest.runtime:.0f}s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

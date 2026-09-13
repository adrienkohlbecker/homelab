#!/usr/bin/env -S uv run
"""
Test runner for Ansible roles using native asyncio parallelism.

This script discovers roles, builds test commands, and executes them concurrently
without relying on GNU parallel. Each child testrole.py tees its own transcript
to `test/out/<machine>.<ubuntu>.<role>.output.ansi`, drops per-run artifacts on
a clean pass, and keeps them for failures under predictable paths. A concise
job log is written to `test/out.tsv`. Partial reruns (--retry-failed,
--only-role, --except-role) merge with the prior log so untouched triples
survive; an unfiltered run replaces the log outright.
"""

import argparse
import asyncio
import contextlib
import csv
import signal
import sys
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from condition_coverage import (
    check_block_coverage,
    check_coverage,
    check_exit_coverage,
    check_include_coverage,
    check_loop_coverage,
    check_result_predicate_coverage,
    check_task_coverage,
    check_until_coverage,
    format_block_gaps,
    format_exit_gaps,
    format_missing_outcomes,
    format_missing_result_predicate_outcomes,
    format_missing_until_outcomes,
    format_unexecuted_loops,
    format_unexecuted_tasks,
    format_unexpanded_includes,
)
from machine import (
    MACHINE_CHOICES,
    UBUNTU_RELEASES,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from matrix import TestCell, build_test_matrix, list_testable_roles
from tabulate import tabulate
from utils import cancel_on_signal, colorize, terminate_subprocess

LOG_FILE = Path("test/out.tsv")
CONDITION_COVERAGE_DIR = Path("test/out/condition_coverage")
JOBLOG_FIELDS = ["Role", "Ubuntu", "Machine", "Runtime", "Exitval", "Started"]
LIVENESS_TICK_SECONDS = 300.0  # 5 minutes

# Flags that describe harness control flow rather than Ansible behavior.
# Keep them out of role_args so they do not leak through to ansible-playbook.
TESTROLE_UNFORWARDED_FLAGS = frozenset(
    {
        "--machine",
        "--ubuntu",
        "--keep",
    }
)


@dataclass(frozen=True)
class JobResult:
    """Holds the outcome of a single role test."""

    cell: TestCell
    runtime: float
    exitval: int
    started_at: str


def _comma_separated(
    *,
    choices: Collection[str] | None = None,
    label: str = "value",
) -> Callable[[str], frozenset[str]]:
    """Build an argparse type for a non-empty comma-separated set."""

    def parse(value: str) -> frozenset[str]:
        values = frozenset(item.strip() for item in value.split(",") if item.strip())
        if not values:
            raise argparse.ArgumentTypeError("must contain at least one value")

        unknown = values.difference(choices or ())
        if choices is not None and unknown:
            raise argparse.ArgumentTypeError(
                f"unknown {label}(s): {', '.join(sorted(unknown))}; valid: {', '.join(sorted(choices))}"
            )
        return values

    return parse


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for selecting machines, roles, and concurrency."""
    parser = argparse.ArgumentParser(
        description="Run Ansible role tests concurrently",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Rerun only roles that failed in the last log; merges into out.tsv",
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
        type=_comma_separated(choices=MACHINE_CHOICES, label="machine profile"),
        default=None,
        metavar="X",
        help="Comma-separated machine filter; only run cells matching these machines "
        "(default: all machines from meta/test.yml)",
    )

    parser.add_argument(
        "--ubuntu",
        type=_comma_separated(choices=UBUNTU_RELEASES, label="Ubuntu codename"),
        default=None,
        metavar="X",
        help="Comma-separated Ubuntu codename filter; only run cells matching these releases "
        "(default: all releases from meta/test.yml)",
    )

    parser.add_argument(
        "--only-role",
        type=_comma_separated(label="role"),
        default=frozenset(),
        metavar="X",
        help="Comma-separated list of roles to run; merges into out.tsv (default: all roles with tasks/main.yml, replacing out.tsv)",
    )

    parser.add_argument(
        "--except-role",
        type=_comma_separated(label="role"),
        default=frozenset(),
        metavar="X",
        help="Comma-separated list of roles to skip; merges into out.tsv. Composes with --only-role (subset, then exclude).",
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


def _read_joblog() -> list[JobResult]:
    """Load the current joblog, returning an empty list when it does not exist."""
    if not LOG_FILE.exists():
        return []

    results: list[JobResult] = []
    with LOG_FILE.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            cell = TestCell(machine=row["Machine"], ubuntu=row["Ubuntu"], role=row["Role"])
            results.append(
                JobResult(
                    cell=cell,
                    runtime=float(row["Runtime"]),
                    exitval=int(row["Exitval"]),
                    started_at=row["Started"],
                )
            )
    return results


async def _emit_liveness(seq: int, cell: TestCell, start_time: float) -> None:
    """Print a periodic 'still running' message until cancelled."""
    while True:
        await asyncio.sleep(LIVENESS_TICK_SECONDS)
        elapsed_min = (time.time() - start_time) / 60.0
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} still running, {elapsed_min:.0f}m elapsed")


async def _run_role(
    seq: int,
    cell: TestCell,
    role_args: Sequence[str],
    semaphore: asyncio.Semaphore,
) -> JobResult:
    """Execute a single role test while respecting the concurrency limit."""
    cmd = [
        "test/testrole.py",
        "--machine",
        cell.machine,
        "--ubuntu",
        cell.ubuntu,
        cell.role,
        *role_args,
    ]
    # testrole.py tees its full transcript via utils.tee_output and owns the
    # file's lifecycle (drops it on success, keeps it on failure under a
    # known path), so testall doesn't need to track or surface log paths.

    async with semaphore:
        start_time = time.time()
        started_at = datetime.fromtimestamp(start_time, tz=UTC).isoformat(timespec="seconds")
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} starting")

        # testrole tees its full transcript into a per-run ANSI log, so its
        # output can stay detached here. It routes stderr through stdout.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        liveness = asyncio.create_task(_emit_liveness(seq, cell, start_time))

        try:
            try:
                await proc.wait()
            except BaseException:
                # 30s gives Machine.stop() enough headroom for its own
                # graceful->SIGKILL escalation (~10-15s worst case for
                # qemu) without SIGKILL'ing testrole.py mid-cleanup and
                # leaking the VM. SIGINT mirrors what Machine.stop()
                # sends to its qemu child so the whole chain reacts the same.
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
        # Per-run log cleanup is testrole's responsibility.
        print(f"[{seq}] {cell.machine}:{cell.ubuntu}:{cell.role} {status} ({runtime:.1f}s)")

    return JobResult(
        cell=cell,
        runtime=runtime,
        exitval=exitval,
        started_at=started_at,
    )


async def run_all(
    cells: list[TestCell],
    role_args: Sequence[str],
    jobs: int,
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
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(_run_role(seq, cell, role_args, semaphore))
                    for seq, cell in enumerate(cells, start=1)
                ]
    except asyncio.CancelledError:
        # When the parent task is cancelled, TaskGroup cascades cancellation
        # into every child and re-raises bare CancelledError on exit. Swallow
        # it here so the caller can see the partial results below.
        cancelled = True

    # Build a result for every (machine, ubuntu, role) so a follow-up
    # `testall.py --retry-failed` retries anything that didn't pass -- whether
    # it ran to completion, was cancelled mid-run, or never got the chance
    # to start (cancel hit before / during TaskGroup setup).
    results: list[JobResult] = []
    for cell, t in zip(cells, tasks, strict=False):
        if t.done() and not t.cancelled() and t.exception() is None:
            results.append(t.result())
        else:
            results.append(_cancelled_result(cell))
    results.extend(_cancelled_result(cell) for cell in cells[len(tasks) :])
    return results, cancelled


def _cancelled_result(cell: TestCell) -> JobResult:
    """Synthetic JobResult for a job interrupted before it could record its own."""
    return JobResult(
        cell=cell,
        runtime=0.0,
        # 130 = 128 + SIGINT, matching what testrole.py emits when it gets
        # cancelled itself, and what main() returns from this script.
        exitval=130,
        started_at="",
    )


def _print_failure_table(failures: list[JobResult]) -> None:
    """Render a table of failed runs with exit codes and runtime."""
    rows = [
        [r.cell.machine, r.cell.ubuntu, r.cell.role, r.exitval, f"{r.runtime:.1f}s"]
        for r in sorted(failures, key=lambda r: (r.cell.machine, r.cell.ubuntu, r.cell.role))
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


def _condition_coverage_reports(roles: Collection[str]) -> list[Path]:
    """Return cell reports belonging to the selected role matrix."""
    return sorted(path for role in roles for path in CONDITION_COVERAGE_DIR.glob(f"*.{role}.jsonl"))


def _clear_condition_coverage_reports(roles: Collection[str]) -> None:
    """Drop prior reports for the roles this run is about to exercise.

    Each Machine only unlinks its own
    ``<machine>.<ubuntu>.<architecture>.<role>.jsonl``, but the gate globs by
    role. A report left behind by a machine since dropped from a role's
    meta/test.yml would merge in as though this run had produced it, so a branch
    that is no longer exercised anywhere would still read as covered.
    """
    for stale in _condition_coverage_reports(roles):
        stale.unlink()


def main() -> int:
    """Entry point for running tests."""
    args = parse_args()

    if args.jobs < 1:
        print("Error: --jobs must be at least 1", file=sys.stderr)
        return 1

    conflicts = [a for a in args.role_args if a.partition("=")[0] in TESTROLE_UNFORWARDED_FLAGS]
    if conflicts:
        print(
            f"Error: these testrole control flags cannot be forwarded through role_args: {conflicts}",
            file=sys.stderr,
        )
        return 1

    prior_results: list[JobResult] = []
    if args.retry_failed:
        if args.machines is not None or args.ubuntu is not None:
            print(
                "Warning: --machines/--ubuntu are ignored with --retry-failed; "
                "the machine and ubuntu of each rerun come from the prior joblog",
                file=sys.stderr,
            )
        prior_results = _read_joblog()
        cells = [result.cell for result in prior_results if result.exitval != 0]
        if not cells:
            print(f"No failed roles recorded in {LOG_FILE}", file=sys.stderr)
            return 0
    else:
        roles = list_testable_roles()
        if not roles:
            print("No roles with tasks/main.yml found", file=sys.stderr)
            return 1

        # Build the meta-driven matrix (same fanout logic as CI), then
        # filter by --machines / --ubuntu when the user wants a subset.
        cells = build_test_matrix(roles)

        if args.machines is not None:
            cells = [cell for cell in cells if cell.machine in args.machines]

        if args.ubuntu is not None:
            cells = [cell for cell in cells if cell.ubuntu in args.ubuntu]

    if args.only_role:
        cells = [cell for cell in cells if cell.role in args.only_role]
        if not cells:
            print("No roles match --only-role", file=sys.stderr)
            return 0

    if args.except_role:
        cells = [cell for cell in cells if cell.role not in args.except_role]
        if not cells:
            print("All roles excluded by --except-role", file=sys.stderr)
            return 0

    if args.list:
        for cell in cells:
            print(f"{cell.machine}\t{cell.ubuntu}\t{cell.role}")
        return 0

    # Reap orphaned workdirs from prior SIGKILL'd / OOM'd / power-cut runs
    # before fanning out, so workers don't trip on stale state and so disk
    # usage doesn't accumulate. Must run before the first Machine is
    # constructed (and thus before any worker subprocess is spawned).
    sweep_stale_workdirs(imagedir_for_host())

    # Clearing is scoped to the gate's own precondition: a partial rerun keeps
    # the prior run's reports, since it does not gate on them either.
    complete_matrix = not args.retry_failed and args.machines is None and args.ubuntu is None
    if complete_matrix:
        _clear_condition_coverage_reports({cell.role for cell in cells})

    # Only partial reruns merge with the prior log; an unfiltered run replaces
    # out.tsv outright so stale triples (deleted roles, old machine/ubuntu
    # scopes) don't linger forever.
    is_partial_rerun = bool(args.retry_failed or args.only_role or args.except_role)
    if is_partial_rerun and not prior_results:
        prior_results = _read_joblog()

    test_start = time.time()
    results, cancelled = asyncio.run(run_all(cells, args.role_args, args.jobs))
    wall_clock = time.time() - test_start

    if results:
        if is_partial_rerun:
            merged = {result.cell: result for result in prior_results}
            for result in results:
                merged[result.cell] = result
            final = list(merged.values())
        else:
            final = results
        _write_joblog(final)

    if cancelled:
        # Synthesized cancellation entries have empty started_at; everything
        # else actually ran (whether it passed or failed).
        completed = sum(1 for r in results if r.started_at)
        msg = (
            f"\nInterrupted, shutting down ({completed}/{len(cells)} completed); "
            f"joblog written to {LOG_FILE} -- rerun with --retry-failed to retry the rest"
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
            f"(parallelism={args.jobs}, longest: {longest.cell.role} on "
            f"{longest.cell.machine}:{longest.cell.ubuntu} at {longest.runtime:.0f}s)",
            file=sys.stderr,
        )

    if complete_matrix:
        selected_roles = {cell.role for cell in cells}
        reports = _condition_coverage_reports(selected_roles)
        try:
            missing = check_coverage(selected_roles, reports)
            unexecuted_loops = check_loop_coverage(selected_roles, reports)
            unexecuted_tasks = check_task_coverage(selected_roles, reports)
            block_gaps = check_block_coverage(selected_roles, reports)
            missing_until_outcomes = check_until_coverage(selected_roles, reports)
            missing_result_predicate_outcomes = check_result_predicate_coverage(selected_roles, reports)
            unexpanded_includes = check_include_coverage(selected_roles, reports)
            exit_gaps = check_exit_coverage(selected_roles, reports)
        except (OSError, ValueError) as exc:
            print(f"Condition coverage report error: {exc}", file=sys.stderr)
            return 1
        if missing:
            print(format_missing_outcomes(missing), file=sys.stderr)
        if unexecuted_loops:
            print(format_unexecuted_loops(unexecuted_loops), file=sys.stderr)
        if unexecuted_tasks:
            print(format_unexecuted_tasks(unexecuted_tasks), file=sys.stderr)
        if block_gaps:
            print(format_block_gaps(block_gaps), file=sys.stderr)
        if missing_until_outcomes:
            print(format_missing_until_outcomes(missing_until_outcomes), file=sys.stderr)
        if missing_result_predicate_outcomes:
            print(
                format_missing_result_predicate_outcomes(missing_result_predicate_outcomes),
                file=sys.stderr,
            )
        if unexpanded_includes:
            print(format_unexpanded_includes(unexpanded_includes), file=sys.stderr)
        if exit_gaps:
            print(format_exit_gaps(exit_gaps), file=sys.stderr)
        if (
            missing
            or unexecuted_loops
            or unexecuted_tasks
            or block_gaps
            or missing_until_outcomes
            or missing_result_predicate_outcomes
            or unexpanded_includes
            or exit_gaps
        ):
            return 1
        print(
            f"Every condition in {len(selected_roles)} selected role(s) evaluated both true and false, "
            "every loop iterated, every task ran, every block path executed, every retry and result predicate "
            "evaluated false and true, every dynamic include expanded, and every early exit took both paths.",
            file=sys.stderr,
        )
    else:
        print(
            "Condition coverage gate skipped for a partial machine/release or retry matrix.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

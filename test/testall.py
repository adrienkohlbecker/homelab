#!/usr/bin/env python3
"""
Test runner for Ansible roles using GNU parallel.

This script discovers roles, builds test commands, and executes them in parallel
using GNU parallel for efficient test execution across multiple machine profiles.
"""

import argparse
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import List


OUT_DIR = Path("test/out")
LOG_FILE = Path("test/out.log")
LOG_FILE_PREV = Path("test/out.log.prev")
_CHILD_PROCESS: subprocess.Popen | None = None


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for selecting machines, roles, and concurrency."""
    parser = argparse.ArgumentParser(
        description="Run Ansible role tests in parallel",
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


def get_failed_roles() -> List[str]:
    """
    Parse the previous parallel job log for failed roles.

    The parallel job log format is tab-separated with the exit code in column 7
    and the executed command (with role name at the end) in the final column.
    """
    if not LOG_FILE.exists():
        return []

    failed_roles: List[str] = []

    # Skip the header row and capture rows with a non-zero exit code.
    with LOG_FILE.open(encoding="utf-8") as log_file:
        for line_no, raw_line in enumerate(log_file):
            if line_no == 0:
                continue
            fields = raw_line.rstrip("\n").split("\t")
            if len(fields) < 7 or fields[6] == "0":
                continue

            # Parallel writes the invoked command as the last column; the role
            # name is the final token in that command.
            command = fields[-1].strip()
            role = command.split()[-1] if command else ""
            if role:
                failed_roles.append(role)

    return list(dict.fromkeys(failed_roles))


def build_parallel_command(
    machines: List[str],
    roles: List[str],
    role_args: List[str],
    jobs: int,
) -> List[str]:
    """Construct the GNU parallel command invocation."""
    cmd = ["parallel", "--jobs", str(jobs), "--joblog", str(LOG_FILE), "--eta", "test/testrole.py", "--machine", "{1}", "{2}", "--checkmode", ">", "test/out/{2}.{1}.ansi"]

    cmd.extend(role_args)

    cmd.append(":::")  # Machine list follows
    cmd.extend(machines)
    cmd.append(":::")  # Role list follows
    cmd.extend(roles)

    return ["bash", "-c", shlex.join(cmd)]


def _install_signal_handlers() -> None:
    """
    Forward SIGINT/SIGTERM to the parallel subprocess and exit cleanly.

    The handlers send the same signal to the process group created for GNU
    parallel so every child role test receives it, then exit with 128+signal.
    """

    def _handler(signum: int, _: object) -> None:
        if _CHILD_PROCESS and _CHILD_PROCESS.poll() is None:
            try:
                os.killpg(_CHILD_PROCESS.pid, signum)
            except ProcessLookupError:
                pass
        # Use conventional 128+signal exit code.
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    """Entry point for running tests."""
    args = parse_args()

    machines = [m.strip() for m in args.machines.split(",") if m.strip()]
    if not machines:
        print("Error: No machines provided to --machines", file=sys.stderr)
        return 1

    if args.jobs < 1:
        print("Error: --jobs must be at least 1", file=sys.stderr)
        return 1

    setup_output_dir()

    if args.only_failed:
        roles = get_failed_roles()
        if not roles:
            print(f"No failed roles recorded in {LOG_FILE}", file=sys.stderr)
            return 0
    else:
        roles = list_roles()
        if not roles:
            print("No roles with tasks/main.yml found", file=sys.stderr)
            return 1

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists():
        if LOG_FILE_PREV.exists():
            LOG_FILE_PREV.unlink()
        LOG_FILE.rename(LOG_FILE_PREV)

    _install_signal_handlers()

    parallel_cmd = build_parallel_command(machines, roles, args.role_args, args.jobs)
    print(shlex.join(parallel_cmd))

    global _CHILD_PROCESS
    _CHILD_PROCESS = subprocess.Popen(
        parallel_cmd,
        # preexec_fn runs in the child just before exec; setpgrp creates a new
        # process group so SIGINT/SIGTERM from our handler propagate to all
        # jobs, but we keep the same session to preserve the controlling TTY.
        preexec_fn=os.setpgrp,
    )

    return _CHILD_PROCESS.wait()

if __name__ == "__main__":
    sys.exit(main())

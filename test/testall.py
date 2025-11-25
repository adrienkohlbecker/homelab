#!/usr/bin/env python3
"""
Test runner for Ansible roles using GNU parallel.

This script discovers roles, builds test commands, and executes them in parallel
using GNU parallel for efficient test execution across multiple machine profiles.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List


OUT_DIR = Path("test/out")
LOG_FILE = Path("test/out.log")
LOG_FILE_PREV = Path("test/out.log.prev")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for selecting machines, roles, and concurrency."""
    parser = argparse.ArgumentParser(
        description="Run Ansible role tests in parallel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--onlyfailed",
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

    # Remaining arguments are forwarded to testrole.sh
    parser.add_argument(
        "role_args",
        nargs="*",
        help="Additional arguments to forward to testrole.sh",
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
    os.environ["OUT_DIR"] = str(OUT_DIR)
    os.environ["LOG_FILE"] = str(LOG_FILE)

    cmd = [
        "parallel",
        "--jobs",
        str(jobs),
        "--joblog",
        str(LOG_FILE),
        "--eta",
        "test/run_role.sh",
        "test/testrole.sh",
        "--machine",
        "{1}",
        "{2}",
    ]

    cmd.extend(role_args)

    cmd.append(":::")  # Machine list follows
    cmd.extend(machines)
    cmd.append(":::")  # Role list follows
    cmd.extend(roles)

    return cmd


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

    if args.onlyfailed:
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

    parallel_cmd = build_parallel_command(machines, roles, args.role_args, args.jobs)

    result = subprocess.run(parallel_cmd, check=False)

    return result.returncode

if __name__ == "__main__":
    sys.exit(main())

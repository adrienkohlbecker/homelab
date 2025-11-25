#!/usr/bin/env python3
"""
Test runner for Ansible roles using GNU parallel.

This script discovers roles, builds test commands, and executes them in parallel
using GNU parallel for efficient test execution across multiple machine profiles.
"""

import argparse
import os
import pathlib
import shlex
import subprocess
import sys
from tempfile import tempdir
from typing import List


OUT_DIR = "test/out"
LOG_FILE = "test/out.log"
LOG_FILE_PREV = "test/out.log.prev"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
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
    """Create output directory and clean up old .ansi files."""
    os.makedirs(OUT_DIR, exist_ok=True)

    # Remove old .ansi files
    out_path = pathlib.Path(OUT_DIR)
    for ansi_file in out_path.glob("*.ansi"):
        ansi_file.unlink()


def list_roles() -> List[str]:
    """List all roles that have a tasks/main.yml file."""
    roles = []
    roles_dir = pathlib.Path("roles")

    if not roles_dir.exists():
        return roles

    for role_dir in sorted(roles_dir.iterdir()):
        if role_dir.is_dir():
            main_yml = role_dir / "tasks" / "main.yml"
            if main_yml.exists():
                roles.append(role_dir.name)

    return roles


def get_failed_roles() -> List[str]:
    """Parse the log file to get roles that failed in the last run."""
    if not os.path.exists(LOG_FILE):
        return []

    failed_roles = []

    with open(LOG_FILE, 'r') as f:
        # Skip header line
        next(f, None)

        for line in f:
            fields = line.strip().split('\t')
            if len(fields) < 7:
                continue

            # Check if exit code is non-zero
            exit_code = fields[6]
            if exit_code != '0':
                # Last field is the role name
                role_machine = fields[-1].strip()
                # Extract role name (last part after space)
                role = role_machine.split()[-1] if role_machine else None
                if role:
                    failed_roles.append(role)

    # Remove duplicates while preserving order
    seen = set()
    unique_failed = []
    for role in failed_roles:
        if role not in seen:
            seen.add(role)
            unique_failed.append(role)

    return unique_failed


def build_parallel_command(
    machines: List[str],
    roles: List[str],
    role_args: List[str],
    jobs: int,
) -> List[str]:
    """Build the GNU parallel command."""
    # Export environment for parallel
    os.environ["OUT_DIR"] = OUT_DIR
    os.environ["LOG_FILE"] = LOG_FILE

    # Build base parallel command
    cmd = [
        "parallel",
        "--jobs", str(jobs),
        "--joblog", LOG_FILE,
        "--eta",
        "test/run_role.sh", "test/testrole.sh", "--machine", "{1}", "{2}"
    ]

    # Add role arguments
    cmd.extend(role_args)

    # Add parallel argument separators and values
    cmd.append(":::")
    cmd.extend(machines)
    cmd.append(":::")
    cmd.extend(roles)

    return cmd

def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Parse machines
    machines = [m.strip() for m in args.machines.split(",") if m.strip()]
    if not machines:
        print("Error: No machines provided to --machines", file=sys.stderr)
        return 1

    # Setup output directory
    setup_output_dir()

    # Get roles
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

    # Backup previous log file
    if os.path.exists(LOG_FILE):
        if os.path.exists(LOG_FILE_PREV):
            os.remove(LOG_FILE_PREV)
        os.rename(LOG_FILE, LOG_FILE_PREV)

    # Build parallel command
    parallel_cmd = build_parallel_command(machines, roles, args.role_args, args.jobs)

    # Wrap in bash to source functions
    bash_script = " ".join(shlex.quote(arg) for arg in parallel_cmd)

    subprocess.run(bash_script, shell = True, executable="/bin/bash")

if __name__ == "__main__":
    sys.exit(main())

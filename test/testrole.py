#!/usr/bin/env -S uv run
"""
Configure and run a single role test with colored output.

Handles argument parsing, environment setup, machine bringup, and log
streaming around an end-to-end converge of one role.
"""

import argparse
import re
import sys
from pathlib import Path

from machine import (
    Machine,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from matrix import (
    DEFAULT_UBUNTU,
    MACHINES,
    UBUNTU_RELEASES,
    RoleTestConfig,
    load_role_test_config,
)
from utils import IdempotenceFailedException, phase, print_line, use_compact_console


def parse_args() -> tuple[argparse.Namespace, list[str], RoleTestConfig]:
    """Parse CLI arguments; unknown args are forwarded to Ansible."""
    parser = argparse.ArgumentParser(
        description="Run a single role test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default=None,
        choices=MACHINES,
        help="Machine profile to run against (default: first roles/<role>/meta/test.yml `machines:` key, else 'lab')",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream every command's output to the terminal; by default it only shows phase status lines, "
        "and the full transcript goes to test/out/<machine>.<ubuntu>.<role>.output.ansi",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30 * 60,
        metavar="SECONDS",
        help="Abort the test if it doesn't complete within this many seconds",
    )
    parser.add_argument(
        "--ubuntu",
        default=DEFAULT_UBUNTU,
        choices=sorted(UBUNTU_RELEASES),
        help="Ubuntu codename of the target image",
    )
    parser.add_argument(
        "--upstream-mirrors",
        action="store_true",
        default=False,
        help="Use public apt/podman mirrors instead of the local Nexus cache (escape hatch when the lab mirror is unreachable)",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse can leave one or more literal "--" tokens at the head of the
    # remainder depending on positional/optional interleaving; strip them all
    # before forwarding to ansible.
    while pass_args and pass_args[0] == "--":
        pass_args = pass_args[1:]

    # --machine defaults to the role's primary machines: entry, which the
    # metadata loader has validated. An explicit CLI value still wins.
    role_config = load_role_test_config(args.role)
    if args.machine is None:
        args.machine = next(iter(role_config.machines))

    return args, pass_args, role_config


# Strip color sequences before matching because their trailing letters can
# prevent the word boundary before `changed=N` from matching.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RECAP_CHANGED_RE = re.compile(r"\bchanged=(\d+)")


def _count_changed_tasks(stdout: list[str]) -> int:
    """Sum `changed=N` across every PLAY RECAP host line in the output."""
    return sum(int(m.group(1)) for line in stdout if (m := _RECAP_CHANGED_RE.search(_ANSI_CSI_RE.sub("", line))))


async def _verify_idempotence(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Re-run the role and fail if any task reports changed."""
    result = await m.ansible_command(site_yml, *pass_args)
    changed = _count_changed_tasks(result.stdout)
    if changed > 0:
        raise IdempotenceFailedException(
            f"Role is not idempotent: {changed} task(s) reported changed on the second run"
        )


async def run_test(
    m: Machine,
    pass_args: list[str],
    *,
    base_prerequisites: bool,
    timeout: int,
) -> None:
    """Provision a machine, run the role under test, and stream output."""

    async with m.session(timeout):
        async with phase("boot"):
            await m.ensure_booted()
            await m.ensure_ssh()
            # SSH opens before the vanilla cloud image finishes cloud-init, so
            # settle it before touching packages or /etc/hosts.
            if m.machine == "minimal":
                await m.ensure_cloud_init()

        if not base_prerequisites:
            print_line(f"Skipping base prerequisites: {m.role!r} declares base_prerequisites: false")

        async with phase("environment"):
            await m.ansible_command(
                str(m.workdir_path / "_environment.yml"),
                "-e",
                f"test_base_prerequisites={str(base_prerequisites).lower()}",
            )
            if m.machine == "minimal" and m.role != "cleanup":
                # Avoid validating the cloud image's newer snapd unit against
                # the older systemd shipped by the fixture.
                await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

        site_yml = str(m.workdir_path / "site.yml")

        # Invoke the setup entrypoint only when the role ships it.
        if Path(f"roles/{m.role}/tasks/_setup.yml").exists():
            async with phase("_setup"):
                await m.ansible_command(site_yml, "-e", "_role_tasks_from=_setup")

        async with phase("check"):
            await m.ansible_command(site_yml, "--check", *pass_args)

        async with phase("converge"):
            await m.ansible_command(site_yml, *pass_args)

        async with phase("idempotence"):
            await _verify_idempotence(site_yml, m, pass_args)

        # Post-role assertions, if the role declares any.
        if Path(f"roles/{m.role}/tasks/_verify.yml").exists():
            async with phase("_verify"):
                await m.ansible_command(site_yml, "-e", "_role_tasks_from=_verify")


def main() -> int:
    """CLI entry point for running a single role test."""

    parsed_args, pass_args, role_config = parse_args()
    if not parsed_args.verbose:
        use_compact_console(parsed_args.role, f"{parsed_args.machine}:{parsed_args.ubuntu}")

    role_main = Path(f"roles/{parsed_args.role}/tasks/main.yml")
    if not role_main.exists():
        print_line(
            f"Error: role '{parsed_args.role}' not found at {role_main}",
            error=True,
        )
        return 1

    # Reap orphaned workdirs from prior SIGKILL'd / OOM'd / power-cut runs
    # before constructing this run's Machine; the .live-file flock check keeps
    # parallel runs from reaping each other's fresh workdirs.
    sweep_stale_workdirs(imagedir_for_host())

    m = Machine(
        machine=parsed_args.machine,
        role=parsed_args.role,
        keep_vm=parsed_args.keep,
        ubuntu_name=parsed_args.ubuntu,
        machine_timeout=parsed_args.timeout,
        upstream_mirrors=parsed_args.upstream_mirrors,
        memory_mb=role_config.memory_mb.get(parsed_args.machine),
    )

    return m.run(
        run_test(m, pass_args, base_prerequisites=role_config.base_prerequisites, timeout=parsed_args.timeout),
        f"{parsed_args.role}.{parsed_args.machine}",
    )


if __name__ == "__main__":
    sys.exit(main())

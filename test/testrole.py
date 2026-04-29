#!/usr/bin/env -S uv run
"""
Configure and run a single role test with colored output.

This replaces the bash shim that previously set environment variables before
dispatching to test/testrole.sh. The heavy lifting still happens in that
shell script; this file handles argument parsing, environment setup, and
log streaming.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

from machine import (
    DEFAULT_UBUNTU,
    Machine,
    PodmanMachine,
    QemuMachine,
    UBUNTU_RELEASES,
    ubuntu_mirrors,
)
from utils import CommandFailedException, IdempotenceFailedException, cancel_on_signal


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    """Parse CLI arguments; unknown args are forwarded to Ansible."""
    parser = argparse.ArgumentParser(
        description="Run a single role test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default="container",
        choices=["container", "minimal", "box", "lab", "pug"],
        help="Machine profile to run against",
    )
    parser.add_argument(
        "--checkmode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run ansible in check mode before the test (default: on; --no-checkmode disables)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test",
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
        "--idempotence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Re-run the role and fail if any task reports changed (default: on)",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse leaves the literal "--" in the remainder; drop it to mirror the
    # old shell parsing behavior.
    if pass_args and pass_args[0] == "--":
        pass_args = pass_args[1:]

    return args, pass_args


async def _configure_apt_sources(m: Machine) -> None:
    """Rewrite apt sources to use local mirrors and refresh package metadata."""
    ubuntu_mirror, ubuntu_mirror_security = ubuntu_mirrors()
    name = m.ubuntu_name
    if name == "jammy":
        # Legacy one-line-per-source list at /etc/apt/sources.list.
        sources = [
            f"deb {ubuntu_mirror} {name} main restricted universe multiverse",
            f"deb {ubuntu_mirror} {name}-updates main restricted universe multiverse",
            f"deb {ubuntu_mirror_security} {name}-security main restricted universe multiverse",
            f"deb {ubuntu_mirror} {name}-backports main restricted universe multiverse",
        ]
        path = "/etc/apt/sources.list"
    else:
        # Noble and beyond ship deb822-style sources.
        sources = [
            "Types: deb",
            f"URIs: {ubuntu_mirror}",
            f"Suites: {name} {name}-updates {name}-backports",
            "Components: main universe restricted multiverse",
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg",
            "",
            "Types: deb",
            f"URIs: {ubuntu_mirror_security}",
            f"Suites: {name}-security",
            "Components: main universe restricted multiverse",
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg",
        ]
        path = "/etc/apt/sources.list.d/ubuntu.sources"

    # Use one shell to avoid repeatedly opening the file and keep quoting simple.
    printf_args = " ".join(f'"{line}"' for line in sources)
    await m.ssh_command("sudo", "bash", "-c", f"printf '%s\\n' {printf_args} > {path}")
    await m.ssh_command("sudo", "apt-get", "update")


_RECAP_CHANGED_RE = re.compile(r"\bchanged=(\d+)")


def _count_changed_tasks(stdout: list[str]) -> int:
    """Sum `changed=N` across every PLAY RECAP host line in the output."""
    return sum(int(match.group(1)) for line in stdout for match in [_RECAP_CHANGED_RE.search(line)] if match)


async def _verify_idempotence(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Re-run the role and fail if any task reports changed."""
    print("Verifying idempotence (re-running the role)...")
    result = await m.ansible_command(site_yml, *pass_args)
    changed = _count_changed_tasks(result.stdout)
    if changed > 0:
        raise IdempotenceFailedException(
            f"Role is not idempotent: {changed} task(s) reported changed on the second run"
        )


async def _run_checkmode(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Run check mode and staged tags when requested."""
    list_tags = await m.ansible_command(site_yml, "--list-tags")

    await m.ansible_command(site_yml, "--check", *pass_args)

    # Some roles split expensive checks into stages; run only those that exist.
    available_tags = "\n".join(list_tags.stdout)
    for stage in ["_check_stage1", "_check_stage2", "_check_stage3", "_check_stage4"]:
        if stage not in available_tags:
            continue
        await m.ansible_command(site_yml, "--tags", stage, *pass_args)
        await m.ansible_command(site_yml, "--check", *pass_args)


async def run_test(parsed_args: argparse.Namespace, pass_args: list[str]) -> None:
    """Provision a machine, run the role under test, and stream output."""

    machine = parsed_args.machine
    role = parsed_args.role
    keep_vm = parsed_args.keep
    checkmode = parsed_args.checkmode
    idempotence = parsed_args.idempotence
    timeout = parsed_args.timeout
    ubuntu_name = parsed_args.ubuntu

    if machine == "container":
        m: Machine = PodmanMachine(machine, role, keep_vm, ubuntu_name=ubuntu_name)
    else:
        m = QemuMachine(machine, role, keep_vm, ubuntu_name=ubuntu_name)

    task = asyncio.current_task()
    assert task is not None

    with cancel_on_signal(task):
        async with m:
            try:
                # Bound the test body with a deadline so a stuck apt/ansible
                # task can't run forever. The keep_vm wait below is outside
                # the timeout on purpose -- it's interactive, not work.
                async with asyncio.timeout(timeout):
                    await m.ensure_booted()
                    print("Booted")

                    await m.ensure_ssh()
                    print("SSH up")

                    await _configure_apt_sources(m)

                    if machine == "minimal":
                        # Fixes systemd-analyze validation error:
                        # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
                        await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

                    try:
                        # Run pre-role fixture playbook(s). _setup is the new
                        # name; _test is the legacy alias still used by most
                        # roles. Roles that have both shouldn't, but if they
                        # do _test runs first to match historical ordering.
                        for hook in ("_test", "_setup"):
                            hook_yml = f"{m.workdir.name}/{hook}.yml"
                            if Path(hook_yml).exists():
                                await m.ansible_command(hook_yml)

                        site_yml = f"{m.workdir.name}/site.yml"
                        if checkmode:
                            await _run_checkmode(site_yml, m, pass_args)

                        await m.ansible_command(site_yml, *pass_args)

                        if idempotence:
                            await _verify_idempotence(site_yml, m, pass_args)

                        # Post-role assertions, if the role declares any.
                        verify_yml = f"{m.workdir.name}/_verify.yml"
                        if Path(verify_yml).exists():
                            await m.ansible_command(verify_yml)

                    except CommandFailedException:
                        print("Command failed")
                        await m.collect_journal()
                        m.print_journal_tail()
                        raise
                    except IdempotenceFailedException:
                        print("Idempotence check failed")
                        raise

            finally:
                # keep_vm runs on success and on CommandFailedException, but
                # not on cancellation -- cancelling() is non-zero only when a
                # SIGINT/SIGTERM-driven cancel is in flight, in which case the
                # user wants out, not an SSH prompt. If a fresh Ctrl+C lands
                # between the check and m.wait(), CancelledError unwinds out
                # through `async with m`'s __aexit__, so m.stop() still runs.
                if keep_vm and not task.cancelling():
                    m.print_ssh_instructions()
                    await m.wait()


def main() -> int:
    """CLI entry point for running a single role test."""

    parsed_args, pass_args = parse_args()

    try:
        asyncio.run(run_test(parsed_args, pass_args))
        return 0
    except CommandFailedException as exc:
        print(exc, file=sys.stderr)
        sys.stderr.write(f"\033[0;41m{parsed_args.role}.{parsed_args.machine} failed\033[0m\n")
        sys.stderr.flush()
        return 1
    except IdempotenceFailedException as exc:
        print(exc, file=sys.stderr)
        sys.stderr.write(f"\033[0;41m{parsed_args.role}.{parsed_args.machine} not idempotent\033[0m\n")
        sys.stderr.flush()
        return 125
    except TimeoutError:
        sys.stderr.write(
            f"\033[0;41m{parsed_args.role}.{parsed_args.machine} timed out after {parsed_args.timeout}s\033[0m\n"
        )
        sys.stderr.flush()
        return 124  # GNU `timeout`'s exit code for "command timed out"
    except asyncio.CancelledError:
        sys.stderr.write("\nInterrupted, shutting down...\n")
        sys.stderr.flush()
        return 130


if __name__ == "__main__":
    sys.exit(main())

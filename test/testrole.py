#!/usr/bin/env python3
"""
Configure and run a single role test with colored output.

This replaces the bash shim that previously set environment variables before
dispatching to test/testrole.sh. The heavy lifting still happens in that
shell script; this file handles argument parsing, environment setup, and
log streaming.
"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import List

from machine import Machine, ubuntu_mirrors, PodmanMachine, QemuMachine, UBUNTU_NAME
from utils import CommandFailedException


def parse_args() -> tuple[argparse.Namespace, List[str]]:
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
        action="store_true",
        help="Run ansible in check mode before the test",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test",
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
    sources = [
        f"deb {ubuntu_mirror} {UBUNTU_NAME} main restricted universe multiverse",
        f"deb {ubuntu_mirror} {UBUNTU_NAME}-updates main restricted universe multiverse",
        f"deb {ubuntu_mirror_security} {UBUNTU_NAME}-security main restricted universe multiverse",
        f"deb {ubuntu_mirror} {UBUNTU_NAME}-backports main restricted universe multiverse",
    ]

    if not ubuntu_mirror or not ubuntu_mirror_security:
        raise RuntimeError("Ubuntu mirror URLs are required to configure apt sources.")

    # Use one shell to avoid repeatedly opening the file and keep quoting simple.
    printf_args = " ".join(f'"{line}"' for line in sources)
    await m.ssh_command("sudo", "bash", "-c", f"printf '%s\\n' {printf_args} > /etc/apt/sources.list")
    await m.ssh_command("sudo", "apt-get", "update")


async def _run_checkmode(site_yml: str, m: Machine, pass_args: List[str]) -> None:
    """Run check mode and staged tags when requested."""
    list_tags: List[str] = []
    await m.ansible_command(site_yml, "--list-tags", captured_lines=list_tags)

    await m.ansible_command(site_yml, "--check", *pass_args)

    # Some roles split expensive checks into stages; run only those that exist.
    available_tags = "\n".join(list_tags)
    for stage in ["_check_stage1", "_check_stage2", "_check_stage3", "_check_stage4"]:
        if stage not in available_tags:
            continue
        await m.ansible_command(site_yml, "--tags", stage, *pass_args)
        await m.ansible_command(site_yml, "--check", *pass_args)


async def run_test(parsed_args: argparse.Namespace, pass_args: List[str]) -> None:
    """Provision a machine, run the role under test, and stream output."""

    machine = parsed_args.machine
    role = parsed_args.role
    keep_vm = parsed_args.keep
    checkmode = parsed_args.checkmode

    if machine == "container":
        m = PodmanMachine(machine, role, keep_vm)
    else:
        m = QemuMachine(machine, role, keep_vm)

    await m.prepare()
    await m.boot()

    try:
        m.ensure_booted()
        print("Booted")

        await m.ensure_ssh()
        print("SSH up")

        await _configure_apt_sources(m)

        if machine == "minimal":
            # Fixes systemd-analyze validation error:
            # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
            await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

        try:
            test_yml = f"{m.workdir.name}/_test.yml"
            if Path(test_yml).exists():
                await m.ansible_command(test_yml)

            site_yml = f"{m.workdir.name}/site.yml"
            if checkmode:
                await _run_checkmode(site_yml, m, pass_args)

            await m.ansible_command(site_yml, *pass_args)

        except CommandFailedException as exc:
            await m.collect_journal()
            raise exc

    finally:
        m.stop()


def main() -> int:
    """CLI entry point for running a single role test."""

    parsed_args, pass_args = parse_args()

    try:
        asyncio.run(run_test(parsed_args, pass_args))
        return 0
    except CommandFailedException as exc:
        sys.stderr.write(f"\033[0;41m{parsed_args.role}.{parsed_args.machine} failed\033[0m\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())

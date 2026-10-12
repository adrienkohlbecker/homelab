#!/usr/bin/env -S uv run
"""
Release migration by boot environment on the Lab integration fixture.

Boots a Lab fixture of the old release, installs the new release beside it with
packer/scripts/install_boot_environment.sh, and switches to it. The old root
first converges the zfs role, as on a production host. The install must leave
the listed shared state unchanged -- partition tables, md array members, ESP
files, firmware boot entries, pool layouts, datasets, swap, the shared kernel
command line, and the running host's runtime state under /run -- and carry the
host identity over: hostid, machine-id, SSH host keys, the login user's ids,
and a CARRY_PATHS file. The new root must boot with every pool and dataset in
place. The boot and refind roles then converge on the new root, which rewrites
the shared ESP and kernel command line, and the test reboots into it once more
before rolling back to the old root. test/playbooks/_boot_environment.yml
holds the assertions.

Not covered: the bare_metal install target (the fixture installs as qemu, to
keep the harness's journal mirror), a single-disk host such as pug, x86_64
unless run on an x86_64 host, and a failure partway through the install.

Exit codes match testrole.py: 0 success, 1 failure, 124 timeout,
130 interrupted.
"""

import argparse
import sys

from machine import Machine, imagedir_for_host, sweep_stale_workdirs
from matrix import UBUNTU_RELEASES
from utils import phase, positive_seconds

# The roles that write the state every boot environment shares: the ESP's
# rEFInd and ZBM, and the kernel command line on rpool/ROOT.
SHARED_BOOT_ROLES = ("boot", "refind")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="from_release",
        default="noble",
        choices=sorted(UBUNTU_RELEASES),
        help="Release the fixture boots (default: %(default)s)",
    )
    parser.add_argument(
        "--to",
        dest="to_release",
        default="resolute",
        choices=sorted(UBUNTU_RELEASES),
        help="Release installed as the new boot environment (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=3600,
        metavar="SECONDS",
        help="Overall timeout (default: %(default)s)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test (for debugging)",
    )
    args = parser.parse_args()
    if args.from_release == args.to_release:
        parser.error("--from and --to must name different releases")
    return args


def run_boot_environment_test(m: Machine, *, timeout: int, to_release: str) -> None:
    playbook = str(m.workdir_path / "_boot_environment.yml")
    environment = str(m.workdir_path / "_environment.yml")
    releases = ("-e", f"boot_environment_from={m.ubuntu_name}", "-e", f"boot_environment_to={to_release}")

    with m.session(timeout):
        m.ensure_booted()
        m.ensure_ssh()
        m.ensure_system_running()

        # The zfs role sets up what a production root carries: the mount
        # generator's cache and the pool properties.
        with phase("_environment"):
            m.ansible_command(environment, "-e", "test_base_prerequisites=true")
        with phase(f"converge zfs ({m.ubuntu_name})"):
            m.ansible_command(str(m.workdir_path / "site.yml"), "-e", "_test_role_under_test=zfs")
        with phase("install"):
            m.ansible_command(playbook, *releases, "-e", "_boot_environment_phase=install")

        # The new root is a fresh install: it needs the fixture's test
        # connectivity before anything converges on it.
        with phase(f"_environment ({to_release})"):
            m.ansible_command(environment)
        for role in SHARED_BOOT_ROLES:
            with phase(f"converge {role} ({to_release})"):
                m.ansible_command(str(m.workdir_path / "site.yml"), "-e", f"_test_role_under_test={role}")

        with phase("rollback"):
            m.ansible_command(playbook, *releases, "-e", "_boot_environment_phase=rollback")


def main() -> int:
    args = parse_args()

    sweep_stale_workdirs(imagedir_for_host())

    m = Machine(
        machine="lab",
        role="_boot_environment",
        keep_vm=args.keep,
        ubuntu_name=args.from_release,
        machine_timeout=args.timeout,
        quiet_ansible=True,
    )

    return m.run(
        lambda: run_boot_environment_test(m, timeout=args.timeout, to_release=args.to_release),
        "boot_environment_test",
    )


if __name__ == "__main__":
    sys.exit(main())

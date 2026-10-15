#!/usr/bin/env -S uv run
"""
Keep-pools rebuild of the Lab integration fixture from a live environment.

Rebuilds a zfs-converged Lab fixture of the old release as the new one with
packer/scripts/provision.sh KEEP_POOLS=1, run from a Minimal stand-in for the
live ISO; test/playbooks/_keep_pools.yml holds the phases and assertions.

Not covered: the bare_metal install target (the fixture installs as qemu, to
keep the harness's journal mirror), a single-disk host such as pug, x86_64
unless run on an x86_64 host, and a failure partway through the install.

Exit codes match testrole.py: 0 success, 1 failure, 124 timeout,
130 interrupted.
"""

import argparse
import sys

from machine import Machine, imagedir_for_host, sweep_stale_workdirs
from matrix import DEFAULT_UBUNTU, OTHER_UBUNTUS, UBUNTU_RELEASES
from utils import phase, positive_seconds

ROLE = "_keep_pools"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="from_release",
        default=next(iter(OTHER_UBUNTUS), None),
        choices=sorted(UBUNTU_RELEASES),
        help="Release of the Lab fixture's root (default: %(default)s)",
    )
    parser.add_argument(
        "--to",
        dest="to_release",
        default=DEFAULT_UBUNTU,
        choices=sorted(UBUNTU_RELEASES),
        help="Release installed beside it, and the live environment's (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=3600,
        metavar="SECONDS",
        help="Overall timeout (default: %(default)s)",
    )
    args = parser.parse_args()
    if args.from_release is None:
        parser.error("the release catalog has no release besides the default to rebuild from")
    if args.from_release == args.to_release:
        parser.error("--from and --to must name different releases")
    return args


def run_keep_pools_test(lab: Machine, *, to_release: str) -> None:
    site = str(lab.workdir_path / "site.yml")
    environment = str(lab.workdir_path / "_environment.yml")
    test_vars = (
        "-e",
        f"keep_pools_from={lab.ubuntu_name}",
        "-e",
        f"keep_pools_to={to_release}",
        "-e",
        f"keep_pools_user={lab.ssh_user}",
        "-e",
        f"keep_pools_hostname={lab.machine}",
    )

    def run_phase(machine: Machine, keep_pools_phase: str) -> None:
        machine.ansible_command(
            str(machine.workdir_path / "_keep_pools.yml"), *test_vars, "-e", f"_keep_pools_phase={keep_pools_phase}"
        )

    with lab.session(lab.machine_timeout):
        lab.ensure_booted()
        lab.ensure_ssh()
        lab.ensure_system_running()

        # The zfs role sets up what a production root carries: the mount
        # generator's cache and the pool properties.
        with phase("_environment"):
            lab.ansible_command(environment, "-e", "test_base_prerequisites=true")
        with phase(f"converge zfs ({lab.ubuntu_name})"):
            lab.ansible_command(site, "-e", "_test_role_under_test=zfs")
        with phase("prepare"):
            run_phase(lab, "prepare")
        with phase("power off"):
            lab.power_off()

        # lab.session() set the deadline the live session must also meet.
        remaining = lab.remaining()
        assert remaining is not None
        live = Machine(
            machine="minimal",
            role=ROLE,
            keep_vm=False,
            ubuntu_name=to_release,
            machine_timeout=int(remaining),
            quiet_ansible=True,
            extra_disks=lab.disk_paths,
            uefi=True,
        )
        with live.session(live.machine_timeout):
            live.ensure_booted()
            live.ensure_ssh()
            live.ensure_cloud_init()
            with phase("install"):
                run_phase(live, "install")
            # A clean exit, so qemu flushes the Lab disks' overlays.
            with phase("power off live environment"):
                live.power_off()

        with phase("boot the Lab disks"):
            lab.boot()
            lab.ensure_booted()
            lab.ensure_ssh()
        with phase("switch"):
            run_phase(lab, "switch")

        # The new root is a fresh install: it needs the fixture's test
        # connectivity before anything converges on it.
        with phase(f"_environment ({to_release})"):
            lab.ansible_command(environment)
        # The roles that write the state every root shares: the ESP's rEFInd
        # and ZBM, and the kernel command line on rpool/ROOT.
        for role in ("boot", "refind"):
            with phase(f"converge {role} ({to_release})"):
                lab.ansible_command(site, "-e", f"_test_role_under_test={role}")

        with phase("rollback"):
            run_phase(lab, "rollback")


def main() -> int:
    args = parse_args()

    sweep_stale_workdirs(imagedir_for_host())

    lab = Machine(
        machine="lab",
        role=ROLE,
        keep_vm=False,
        ubuntu_name=args.from_release,
        machine_timeout=args.timeout,
        quiet_ansible=True,
    )

    return lab.run(
        lambda: run_keep_pools_test(lab, to_release=args.to_release),
        "keep_pools_test",
    )


if __name__ == "__main__":
    sys.exit(main())

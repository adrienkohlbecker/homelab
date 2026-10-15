#!/usr/bin/env -S uv run
"""
Keep-pools rebuild of the Lab integration fixture from a live environment.

Boots a Lab fixture of the old release and converges the zfs role on it, as on
a production host, then powers it off. A Minimal cloud image of the new
release stands in for the live ISO: it boots with the Lab disks attached and
runs packer/scripts/provision.sh with KEEP_POOLS=1, then reruns it, which must
refuse before writing. The Lab disks then boot alone. The old root must still
be the one that boots, with the shared state unchanged -- partition tables, md
array members, ESP files, firmware boot entries, pool layouts, datasets, swap,
the shared kernel command line, and the host identity. Switched to, the new
root must boot with every pool and dataset in place and the old root's
identity: hostid, machine-id, SSH host keys, and the login user's ids. The
boot and refind roles then converge on the new root, which rewrites the shared
ESP and kernel command line, and the test reboots into it once more before
rolling back to the old root. test/playbooks/_keep_pools.yml holds the
assertions.

Not covered: the bare_metal install target (the fixture installs as qemu, to
keep the harness's journal mirror), a single-disk host such as pug, x86_64
unless run on an x86_64 host, and a failure partway through the install.

Exit codes match testrole.py: 0 success, 1 failure, 124 timeout,
130 interrupted.
"""

import argparse
import sys

from machine import Machine, imagedir_for_host, sweep_stale_workdirs
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import phase, positive_seconds

# The roles that write the state every root shares: the ESP's rEFInd and ZBM,
# and the kernel command line on rpool/ROOT.
SHARED_BOOT_ROLES = ("boot", "refind")
ROLE = "_keep_pools"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="from_release",
        default=next(codename for codename in UBUNTU_RELEASES if codename != DEFAULT_UBUNTU),
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
    if args.from_release == args.to_release:
        parser.error("--from and --to must name different releases")
    return args


def run_keep_pools_test(lab: Machine, *, timeout: int, to_release: str) -> None:
    playbook = "_keep_pools.yml"
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

    def lab_playbook(keep_pools_phase: str) -> None:
        lab.ansible_command(str(lab.workdir_path / playbook), *test_vars, "-e", f"_keep_pools_phase={keep_pools_phase}")

    with lab.session(timeout):
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
            lab_playbook("prepare")
        with phase("power off"):
            lab.power_off()

        live = Machine(
            machine="minimal",
            role=ROLE,
            keep_vm=False,
            ubuntu_name=to_release,
            machine_timeout=int(lab.remaining() or timeout),
            quiet_ansible=True,
            extra_disks=lab.disk_paths,
        )
        with live.session(live.machine_timeout):
            live.ensure_booted()
            live.ensure_ssh()
            live.ensure_cloud_init()
            with phase("install"):
                live.ansible_command(str(live.workdir_path / playbook), *test_vars, "-e", "_keep_pools_phase=install")
            # A clean exit, so qemu flushes the Lab disks' overlays.
            with phase("power off live environment"):
                live.power_off()

        with phase("boot the Lab disks"):
            lab.boot()
            lab.ensure_booted()
            lab.ensure_ssh()
        with phase("switch"):
            lab_playbook("switch")

        # The new root is a fresh install: it needs the fixture's test
        # connectivity before anything converges on it.
        with phase(f"_environment ({to_release})"):
            lab.ansible_command(environment)
        for role in SHARED_BOOT_ROLES:
            with phase(f"converge {role} ({to_release})"):
                lab.ansible_command(site, "-e", f"_test_role_under_test={role}")

        with phase("rollback"):
            lab_playbook("rollback")


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
        lambda: run_keep_pools_test(lab, timeout=args.timeout, to_release=args.to_release),
        "keep_pools_test",
    )


if __name__ == "__main__":
    sys.exit(main())

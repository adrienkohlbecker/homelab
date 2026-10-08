#!/usr/bin/env -S uv run
"""
Full site.yml converge on the Lab integration fixture.

Boots a Lab QEMU fixture, prepares test-only connectivity and credentials, then
runs the real site.yml with --limit lab.
Catches role-ordering and cross-role interaction bugs that per-role tests miss.

--check runs the same full site.yml in ansible check mode (a dry run) instead
of a real converge: it makes no changes, so it needs no post-converge settle or
poweroff dance, and it exercises the whole playbook's check-mode safety (every
integration role's template rendering and check-mode gating) through the
full-site role ladder in one pass -- something the per-role cells, each running
one role in isolation, can't.

Exit codes match testrole.py: 0 success, 1 converge failure, 124 timeout,
130 interrupted.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from machine import (
    SYSTEM_RUNNING_WAIT_TIMEOUT,
    Machine,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import CheckFailedException, CommandFailedException, print_line

# Backstop for the post-poweroff wait. With the settle gate below, the fleet is
# healthy before SIGTERM, so a real poweroff drains in well under a minute (the
# nexus JVM is the slowest at ~35s). A poweroff that runs much longer means a
# container wedged on its --stop-timeout (e.g. an app SIGTERM'd mid-bootstrap,
# which a .NET/Java host rides to SIGKILL) -- surface that as a fast failure
# rather than letting it silently burn the overall --timeout. 120s clears the
# legitimate drain with wide margin while still catching a genuine wedge. The
# serial console (boot.ansi) records which stop jobs hung.
POWEROFF_TIMEOUT = 120

# The fixture's DNS reboot guard holds a root shutdown block lock on its only
# DNS server. Start poweroff.target through PID1, which has no inhibitors and
# needs no polkit, rather than asking logind to skip the lock.
POWEROFF_COMMAND = ("sudo", "systemctl", "start", "--no-block", "--job-mode=replace-irreversibly", "poweroff.target")


# podman runs each container's --health-startup-cmd and periodic --health-cmd
# as transient units named from the container ID. Podman 5 adds a random hex
# suffix to keep names unique across runs. Probes exit non-zero while the
# container is not (yet) healthy; those failures are the mechanism working.
PODMAN_HEALTHCHECK_UNIT = re.compile(r"^[0-9a-f]{64}(?:-startup)?(?:-[0-9a-f]+)?\.(?:service|timer)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ubuntu",
        default=DEFAULT_UBUNTU,
        choices=sorted(UBUNTU_RELEASES),
        help="Ubuntu release codename (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3000,
        metavar="SECONDS",
        help="Overall timeout for boot + converge (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run site.yml in ansible check mode (dry run; makes no changes)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the machine running after the test (for debugging)",
    )
    return parser.parse_args()


def report_restarted_units(m: Machine, pid1_journal: list[str]) -> list[str]:
    """Print the logs of units that failed during this boot, and name them.

    A unit that crash-loops before succeeding still ends up `active`, so
    `systemctl is-system-running` reports `running` and only the attempt that
    worked shows in `systemd-analyze blame`. Jellyfin spent months never
    starting in the settled boot behind exactly that. PID 1 reports the
    failure but not the unit's own output, which is where the reason lives.
    """
    failed: list[str] = []
    for line in pid1_journal:
        match = re.search(
            r"(\S+\.(?:service|socket|timer|mount|path|swap)): (?:Main process exited|Failed with result|Scheduled restart)",
            line,
        )
        if not match:
            continue
        unit = match.group(1)
        if unit not in failed and not PODMAN_HEALTHCHECK_UNIT.match(unit):
            failed.append(unit)
    if not failed:
        return []
    print_line(f"Units that failed or restarted this boot: {' '.join(failed)}", error=True)
    for unit in failed[:5]:
        logs = m.ssh_command("journalctl", "--boot", "--no-pager", "--output=short-monotonic", "-u", unit, check=False)
        body = "\n".join(logs.stdout[-30:]).rstrip() or "(unavailable)"
        print_line(f"{unit} log:\n{body}")
    return failed


def run_site_test(m: Machine, *, timeout: int, check_mode: bool = False) -> None:
    with m.session(timeout):
        m.ensure_booted()
        print_line("Booted")

        m.ensure_ssh()
        print_line("SSH up")

        m.ensure_system_running()

        print_line("Preparing test environment")
        m.ansible_command(str(m.workdir_path / "_environment.yml"))

        if check_mode:
            # A production check starts with the persistent services dataset
            # already mounted. Seed that invariant outside --check so the
            # identity roles can safely inspect their durable key paths; a
            # fresh check may predict the dataset creation but cannot mount it.
            print_line("Preparing site check prerequisites")
            m.ansible_command(
                str(m.workdir_path / "site.yml"),
                "-e",
                "_test_role_under_test=services",
            )

        staged = m.workdir_path / "site.yml"
        shutil.copy(Path("site.yml"), staged)
        # site.yml imports bunk.yml at parse time; the fixture has no bunk
        # host, so the play only needs to resolve.
        shutil.copy(Path("bunk.yml"), m.workdir_path / "bunk.yml")

        label = "check" if check_mode else "converge"
        print_line(f"Running site.yml {label}")
        try:
            extra = ["--check"] if check_mode else []
            m.ansible_command(str(staged), *extra)
        except CommandFailedException:
            print_line(f"Site {label} failed")
            raise

        print_line(f"Site {label} passed")
        restarted: list[str] = []
        # Check mode makes no changes: nothing was installed, no
        # kernel upgrade set reboot-required, no container is
        # mid-bootstrap -- so the settle gate and poweroff dance
        # below (all about draining a live converge cleanly) don't
        # apply. Fall through and let the context manager tear the
        # guest down.
        if not check_mode and not m.keep_vm:
            # The converge's final [Reboot check] play reboots when a
            # kernel upgrade set /var/run/reboot-required, so the fleet is
            # mid-restart here: ansible returns once SSH is back, but the
            # container units (Type=notify, --sdnotify=healthy) are still
            # activating. Powering off now SIGTERMs apps mid-bootstrap, and
            # a .NET/Java host that gets SIGTERM before it finishes starting
            # never drains -- it rides to its --stop-timeout and is SIGKILLed,
            # wedging the whole poweroff. Wait for systemd to finish starting
            # (every unit active-or-failed, none left activating) so the
            # poweroff drains cleanly, mirroring the boot-time gate above.
            # Don't hard-fail on a non-running state: some services are
            # legitimately degraded under the test harness (e.g. z2m has no
            # live adapter) and waiting longer won't change that -- the point
            # is only that nothing is still mid-bootstrap.
            settle_rc, settle_state = m.wait_system_running()
            if settle_rc == 124:
                raise CheckFailedException(
                    f"systemd did not finish starting within {SYSTEM_RUNNING_WAIT_TIMEOUT}s of the converge "
                    "(a unit is stuck activating -- see the serial console and journal mirror)"
                )
            if settle_state == "running":
                print_line(f"Fleet settled: {settle_state}")
            else:
                print_line(f"Fleet settled as {settle_state!r}; failed units:\n{m.failed_units()}")
            # PID 1's journal drives the restart audit, so a failed query must
            # raise rather than read as "nothing failed".
            journal = m.ssh_command("journalctl", "--boot", "--no-pager", "--output=short-monotonic", "_PID=1")
            restarted = report_restarted_units(m, journal.stdout)

            m.ssh_command(*POWEROFF_COMMAND, check=False)
            # Bound the shutdown wait separately from the converge
            # budget: a wedged stop job must surface as a failure,
            # not eat the remaining --timeout. collect_failure_
            # artifacts is best-effort (SSH is usually gone by now);
            # the serial console is captured regardless.
            try:
                m.wait(POWEROFF_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise CheckFailedException(
                    f"poweroff did not complete within {POWEROFF_TIMEOUT}s after a passed converge "
                    "(a stop job wedged on its TimeoutStopSec -- see the serial console for which units)"
                ) from None

        # Raised after the guest is down so the shutdown path and its
        # artifacts are still exercised. A unit that recovers on a retry is
        # still a unit that could not start the fleet as converged.
        if restarted:
            raise CheckFailedException(
                f"units failed during the settled boot: {' '.join(restarted)} "
                "(each recovered later, so the fleet still reports running -- "
                "see the per-unit logs above)"
            )


def main() -> int:
    args = parse_args()

    sweep_stale_workdirs(imagedir_for_host())

    m = Machine(
        machine="lab",
        # Distinct artifact names keep simultaneous check and converge logs
        # separate.
        role="_site_check" if args.check else "_site_test",
        keep_vm=args.keep,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        # The converge runs dozens of services; its 12-GiB guest books three
        # cells' worth of a shared 16-vCPU/32-GiB CI worker
        # (capacity_per_instance). Check mode renders the same site without
        # starting them.
        vcpus=None if args.check else 8,
        memory_mb=None if args.check else 12288,
        quiet_ansible=True,
    )

    return m.run(lambda: run_site_test(m, timeout=args.timeout, check_mode=args.check), "site_test")


if __name__ == "__main__":
    sys.exit(main())

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
import asyncio
import re
import shutil
import sys
import traceback
from pathlib import Path

from machine import (
    Machine,
    MachineRunOptions,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import (
    CommandFailedException,
    print_line,
    tee_output,
)

# Backstop for the post-poweroff wait. With the settle gate below, the fleet is
# healthy before SIGTERM, so a real poweroff drains in well under a minute (the
# nexus JVM is the slowest at ~35s). A poweroff that runs much longer means a
# container wedged on its --stop-timeout (e.g. an app SIGTERM'd mid-bootstrap,
# which a .NET/Java host rides to SIGKILL) -- surface that as a fast failure
# rather than letting it silently burn the overall --timeout. 120s clears the
# legitimate drain with wide margin while still catching a genuine wedge. The
# serial console (boot.ansi) records which stop jobs hung.
POWEROFF_TIMEOUT = 120

# The converge runs dozens of services; its 12-GiB guest books three cells'
# worth of a shared 16-vCPU/32-GiB CI worker (capacity_per_instance). Check
# mode renders the same site without starting them.
SITE_CONVERGE_OPTIONS = MachineRunOptions(vcpus=8, memory_mb=12288, quiet_ansible=True)
SITE_CHECK_OPTIONS = MachineRunOptions(quiet_ansible=True)


class PoweroffTimeoutError(Exception):
    """The guest failed to power off within POWEROFF_TIMEOUT after a passed converge."""


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
    parser.add_argument(
        "--workdir-parent",
        type=Path,
        default=None,
        metavar="PATH",
        help="Parent directory for the per-run workdir (default: imagedir)",
    )
    return parser.parse_args()


async def print_boot_profile(m: Machine) -> None:
    """Print where the settled boot spent its time.

    The settle waits out the whole post-reboot fleet start, so the slowest
    units and the critical chain are what bound it. Diagnostic only: a
    failure here never fails the test.
    """
    blame = await m.ssh_command("systemd-analyze", "blame", "--no-pager", check=False)
    slowest = "\n".join(blame.stdout[:25]).rstrip() or "(unavailable)"
    print_line(f"Slowest units this boot:\n{slowest}")
    chain = await m.ssh_command("systemd-analyze", "critical-chain", "--no-pager", check=False)
    print_line("Boot critical chain:\n" + ("\n".join(chain.stdout).rstrip() or "(unavailable)"))
    # critical-chain skips units without an active-enter timestamp (oneshots
    # without RemainAfterExit, failed units), so it can credit multi-user.target
    # to a unit that finished minutes earlier. PID 1's own log shows what really
    # completed last before the target.
    journal = await m.ssh_command(
        "journalctl", "--boot", "--no-pager", "--output=short-monotonic", "_PID=1", check=False
    )
    reached = [i for i, line in enumerate(journal.stdout) if "Reached target multi-user.target" in line]
    if reached:
        tail = journal.stdout[max(0, reached[-1] - 40) : reached[-1] + 1]
        print_line("systemd log before multi-user.target:\n" + "\n".join(tail))
    await print_restarted_units(m, journal.stdout)


async def print_restarted_units(m: Machine, pid1_journal: list[str]) -> None:
    """Print the logs of units that failed or restarted during this boot.

    A unit that crash-loops before succeeding still reaches `active`, so the
    test passes and `systemd-analyze blame` only shows the attempt that
    worked. PID 1 reports the failure but not the unit's own output, which is
    where the reason lives. Diagnostic only: never fails the test.
    """
    failed = []
    for line in pid1_journal:
        match = re.search(r"(\S+\.service): (?:Main process exited|Failed with result|Scheduled restart)", line)
        if match and match.group(1) not in failed:
            failed.append(match.group(1))
    if not failed:
        return
    print_line(f"Units that failed or restarted this boot: {' '.join(failed)}")
    for unit in failed[:5]:
        logs = await m.ssh_command(
            "journalctl", "--boot", "--no-pager", "--output=short-monotonic", "-u", unit, check=False
        )
        body = "\n".join(logs.stdout[-30:]).rstrip() or "(unavailable)"
        print_line(f"{unit} log:\n{body}")


async def run_site_test(m: Machine, *, timeout: int, check_mode: bool = False) -> None:
    async with m.session(timeout):
        await m.ensure_booted()
        print_line("Booted")

        await m.ensure_ssh()
        print_line("SSH up")

        await m.ensure_system_running()

        print_line("Preparing test environment")
        await m.ansible_command(str(m.workdir_path / "_environment.yml"))

        if check_mode:
            # A production check starts with the persistent services dataset
            # already mounted. Seed that invariant outside --check so the
            # identity roles can safely inspect their durable key paths; a
            # fresh check may predict the dataset creation but cannot mount it.
            print_line("Preparing site check prerequisites")
            await m.ansible_command(
                str(m.workdir_path / "site.yml"),
                "-e",
                "_test_role_under_test=services",
            )

        staged = m.workdir_path / "site.yml"
        shutil.copy(Path("site.yml"), staged)

        label = "check" if check_mode else "converge"
        print_line(f"Running site.yml {label}")
        try:
            extra = ["--check"] if check_mode else []
            await m.ansible_command(str(staged), *extra)
        except CommandFailedException:
            print_line(f"Site {label} failed")
            raise

        print_line(f"Site {label} passed")
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
            settle = await m.ssh_command("systemctl", "is-system-running", "--wait", check=False)
            settle_state = "\n".join(settle.stdout).strip()
            if settle_state == "running":
                print_line(f"Fleet settled: {settle_state}")
            else:
                failed = await m.ssh_command("systemctl", "--failed", "--no-legend", check=False)
                failed_units = "\n".join(failed.stdout).rstrip() or "(none)"
                print_line(f"Fleet settled as {settle_state!r}; failed units:\n{failed_units}")
            await print_boot_profile(m)

            await m.ssh_command("sudo", "systemctl", "--check-inhibitors=no", "poweroff", check=False)
            # Bound the shutdown wait separately from the converge
            # budget: a wedged stop job must surface as a failure,
            # not eat the remaining --timeout. collect_failure_
            # artifacts is best-effort (SSH is usually gone by now);
            # the serial console is captured regardless.
            try:
                await asyncio.wait_for(m.wait(), timeout=POWEROFF_TIMEOUT)
            except TimeoutError:
                print_line(
                    f"Guest did not power off within {POWEROFF_TIMEOUT}s after a passed converge",
                    error=True,
                )
                raise PoweroffTimeoutError(
                    f"poweroff did not complete within {POWEROFF_TIMEOUT}s "
                    "(a stop job wedged on its TimeoutStopSec -- see the "
                    "serial console for which units)"
                ) from None


def main() -> int:
    args = parse_args()

    sweep_stale_workdirs(imagedir_for_host())

    m = Machine(
        machine="lab",
        # Distinct artifact names keep simultaneous check and converge logs
        # separate; run behavior is carried explicitly by run_options.
        role="_site_check" if args.check else "_site_test",
        keep_vm=args.keep,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        workdir_parent=args.workdir_parent,
        run_options=SITE_CHECK_OPTIONS if args.check else SITE_CONVERGE_OPTIONS,
    )

    rc = 0
    with tee_output(m.output_file):
        try:
            asyncio.run(run_site_test(m, timeout=args.timeout, check_mode=args.check))
        except CommandFailedException as exc:
            print_line(str(exc), error=True)
            print_line("site_test failed", error=True)
            rc = 1
        except PoweroffTimeoutError as exc:
            print_line(str(exc), error=True)
            print_line("site_test failed", error=True)
            rc = 1
        except TimeoutError:
            print_line(f"site_test timed out after {args.timeout}s", error=True)
            rc = 124
        except asyncio.CancelledError:
            print_line("\nInterrupted, shutting down...")
            rc = 130
        except Exception:
            print_line(traceback.format_exc().rstrip(), error=True)
            print_line("site_test crashed", error=True)
            rc = 1

    if rc == 0:
        m.cleanup_logs()

    return rc


if __name__ == "__main__":
    sys.exit(main())

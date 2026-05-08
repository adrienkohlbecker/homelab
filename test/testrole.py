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
import contextlib
import re
import sys
import time
from pathlib import Path

from machine import (
    DEFAULT_UBUNTU,
    MACHINE_CHOICES,
    PEAK_KB_SENTINEL_PREFIX,
    Machine,
    QemuMachine,
    UBUNTU_RELEASES,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from utils import CommandFailedException, IdempotenceFailedException, cancel_on_signal, print_line, tee_output

# Benchmark mode: harness phase timings + per-task ansible profiling. Off by
# default; flip on with --benchmark when investigating why a role is slow.
_BENCHMARK = False
_PHASE_TIMINGS: list[tuple[str, float]] = []


@contextlib.asynccontextmanager
async def _phase(label: str):
    if not _BENCHMARK:
        yield
        return
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        _PHASE_TIMINGS.append((label, dt))
        print_line(f"[phase] {label}: {dt:.1f}s")


def _print_phase_summary() -> None:
    if not _BENCHMARK or not _PHASE_TIMINGS:
        return
    total = sum(dt for _, dt in _PHASE_TIMINGS)
    print_line("=" * 60)
    print_line("PHASE TIMINGS")
    print_line("=" * 60)
    width = max(len(label) for label, _ in _PHASE_TIMINGS)
    for label, dt in _PHASE_TIMINGS:
        pct = (dt / total * 100) if total > 0 else 0.0
        print_line(f"  {label:<{width}}  {dt:6.1f}s  ({pct:4.1f}%)")
    print_line(f"  {'TOTAL':<{width}}  {total:6.1f}s")
    print_line("=" * 60)


def _positive_int(value: str) -> int:
    """argparse type for flags that must be a positive integer."""
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {n}")
    return n


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    """Parse CLI arguments; unknown args are forwarded to Ansible."""
    parser = argparse.ArgumentParser(
        description="Run a single role test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default="box",
        choices=MACHINE_CHOICES,
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
        type=_positive_int,
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
    parser.add_argument(
        "--keep-logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep output/boot/journal logs after a successful run (default: on; --no-keep-logs deletes them, used by testall.py)",
    )
    parser.add_argument(
        "--upstream-mirrors",
        action="store_true",
        default=False,
        help="Use public apt/podman mirrors instead of the local Nexus cache (escape hatch when the lab mirror is unreachable)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        default=False,
        help="Print harness phase timings and enable ansible's profile_tasks callback for per-task timing",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse can leave one or more literal "--" tokens at the head of the
    # remainder depending on positional/optional interleaving; strip them all
    # to mirror the old shell parsing behavior.
    while pass_args and pass_args[0] == "--":
        pass_args = pass_args[1:]

    return args, pass_args


_RECAP_CHANGED_RE = re.compile(r"\bchanged=(\d+)")


def _count_changed_tasks(stdout: list[str]) -> int:
    """Sum `changed=N` across every PLAY RECAP host line in the output."""
    return sum(int(m.group(1)) for line in stdout if (m := _RECAP_CHANGED_RE.search(line)))


async def _verify_idempotence(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Re-run the role and fail if any task reports changed."""
    print_line("Verifying idempotence (re-running the role)...")
    async with _phase("idempotence rerun"):
        result = await m.ansible_command(site_yml, *pass_args)
    changed = _count_changed_tasks(result.stdout)
    if changed > 0:
        raise IdempotenceFailedException(f"Role is not idempotent: {changed} task(s) reported changed on the second run")


async def _run_checkmode(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Dry-run the role on a fresh system before mutating anything."""
    async with _phase("checkmode --check"):
        await m.ansible_command(site_yml, "--check", *pass_args)


async def run_test(
    m: Machine,
    pass_args: list[str],
    *,
    checkmode: bool,
    idempotence: bool,
    timeout: int,
) -> None:
    """Provision a machine, run the role under test, and stream output."""

    task = asyncio.current_task()
    assert task is not None

    # When --keep is set and the deadline fires, we absorb the resulting
    # cancel so async with m doesn't tear down the VM and the user can
    # still SSH in to debug. We re-surface TimeoutError after the wait so
    # main() reports rc=124 regardless.
    timer_absorbed = False

    with cancel_on_signal(task):
        # Bound the whole test (prepare, boot, body) with a deadline so a
        # stuck qemu-img / apt / ansible task can't run forever. The keep_vm
        # wait below disables the deadline via reschedule(None) once the
        # body finishes -- the SSH session is interactive, not work.
        async with asyncio.timeout(timeout) as timeout_cm:
            async with m:
                try:
                    try:
                        async with _phase("boot"):
                            await m.ensure_booted()
                        print_line("Booted")

                        async with _phase("ssh wait"):
                            await m.ensure_ssh()
                        print_line("SSH up")

                        # Per-variant disk setup (lab / pug create their
                        # extra ZFS pools here). No-op for variants whose
                        # spec leaves disk_setup_script unset.
                        async with _phase("disk setup"):
                            await m.run_disk_setup()

                        # Mirror setup playbook: apt sources, podman registries,
                        # /etc/pip.conf, /etc/uv/uv.toml. Routes everything
                        # through the lab Nexus when nexus_url is set
                        # (group_vars/test.yml), upstream when --upstream-mirrors
                        # clears it.
                        async with _phase("mirrors playbook"):
                            await m.ansible_command(f"{m.workdir.name}/_mirrors.yml")

                        if m.machine == "minimal":
                            # Fixes systemd-analyze validation error:
                            # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
                            await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

                        # Pre-role fixture playbook. The hook playbook is
                        # static at test/playbooks/_setup.yml; we invoke it
                        # only when the role under test ships
                        # roles/<role>/tasks/_setup.yml for it to import.
                        if Path(f"roles/{m.role}/tasks/_setup.yml").exists():
                            async with _phase("hook _setup.yml"):
                                await m.ansible_command(f"{m.workdir.name}/_setup.yml")

                        site_yml = f"{m.workdir.name}/site.yml"
                        if checkmode:
                            await _run_checkmode(site_yml, m, pass_args)

                        async with _phase("main apply"):
                            await m.ansible_command(site_yml, *pass_args)

                        if idempotence:
                            await _verify_idempotence(site_yml, m, pass_args)

                        # Post-role assertions, if the role declares any.
                        if Path(f"roles/{m.role}/tasks/_verify.yml").exists():
                            async with _phase("verify.yml"):
                                await m.ansible_command(f"{m.workdir.name}/_verify.yml")

                    except CommandFailedException:
                        print_line("Command failed")
                        # Best-effort: a journal-collection failure must not
                        # shadow the underlying CommandFailedException.
                        with contextlib.suppress(Exception):
                            await m.collect_journal()
                            m.print_file_tail(m.journal_file)
                        raise
                    except IdempotenceFailedException:
                        print_line("Idempotence check failed")
                        raise
                    except asyncio.CancelledError:
                        # The deadline fired (asyncio.timeout cancels the task
                        # to surface TimeoutError). With --keep we want the VM
                        # to stay up for debugging, so absorb the cancel and
                        # fall through to the keep_vm wait below.
                        if m.keep_vm and timeout_cm.expired() and task.cancelling():
                            task.uncancel()
                            timer_absorbed = True
                            print_line(
                                f"Timed out after {timeout}s; --keep set, dropping to SSH for debug",
                            )
                        else:
                            raise

                finally:
                    # keep_vm waits for the user on success, CommandFailedException,
                    # IdempotenceFailedException, and (after absorbing the timer
                    # above) on TimeoutError. It does NOT run on user-driven
                    # cancellation -- Ctrl+C means out, not an SSH prompt.
                    # reschedule(None) makes the wait unbounded; on a fired
                    # timer the cm rejects reschedule, but the timer is already
                    # spent so the wait remains effectively unbounded anyway.
                    if m.keep_vm and not task.cancelling():
                        with contextlib.suppress(RuntimeError):
                            timeout_cm.reschedule(None)
                        m.print_ssh_instructions()
                        await m.wait()

    if timer_absorbed:
        # Re-surface the timeout we silenced so main() reports rc=124. If a
        # user Ctrl+C broke the wait above, asyncio.timeout's __aexit__ has
        # already converted that cancel into TimeoutError before reaching
        # this line, so the manual raise only covers natural exits.
        raise TimeoutError(f"Test timed out after {timeout}s")


def main() -> int:
    """CLI entry point for running a single role test."""

    parsed_args, pass_args = parse_args()

    if parsed_args.benchmark:
        global _BENCHMARK
        _BENCHMARK = True
        # profile_tasks tags every TASK header with elapsed time and prints a
        # TASKS RECAP at end of each play; env var picks it up for every
        # ansible-playbook subprocess without editing ansible.cfg.
        import os

        os.environ["ANSIBLE_CALLBACKS_ENABLED"] = "profile_tasks"

    role_main = Path(f"roles/{parsed_args.role}/tasks/main.yml")
    if not role_main.exists():
        print_line(
            f"Error: role '{parsed_args.role}' not found at {role_main}",
            error=True,
        )
        return 1

    # Reap orphaned workdirs from prior SIGKILL'd / OOM'd / power-cut runs
    # before constructing this run's QemuMachine. testall.py also sweeps once
    # before fanning out; the mtime grace inside sweep_stale_workdirs keeps
    # parallel workers from racing on each other's freshly-minted workdirs.
    sweep_stale_workdirs(imagedir_for_host())

    # Machine.wrapper_timeout layers WRAPPER_GRACE_SECONDS on top of this so
    # the inner `timeout` wrapper outlasts the Python deadline.
    m: Machine = QemuMachine(
        machine=parsed_args.machine,
        role=parsed_args.role,
        keep_vm=parsed_args.keep,
        ubuntu_name=parsed_args.ubuntu,
        machine_timeout=parsed_args.timeout,
        upstream_mirrors=parsed_args.upstream_mirrors,
    )

    rc = 0
    with tee_output(m.output_file):
        try:
            asyncio.run(
                run_test(
                    m,
                    pass_args,
                    checkmode=parsed_args.checkmode,
                    idempotence=parsed_args.idempotence,
                    timeout=parsed_args.timeout,
                )
            )
        except CommandFailedException as exc:
            print_line(str(exc), error=True)
            print_line(f"{parsed_args.role}.{parsed_args.machine} failed", error=True)
            rc = 1
        except IdempotenceFailedException as exc:
            print_line(str(exc), error=True)
            print_line(f"{parsed_args.role}.{parsed_args.machine} not idempotent", error=True)
            rc = 125
        except TimeoutError:
            print_line(
                f"{parsed_args.role}.{parsed_args.machine} timed out after {parsed_args.timeout}s",
                error=True,
            )
            rc = 124  # GNU `timeout`'s exit code for "command timed out"
        except asyncio.CancelledError:
            print_line("\nInterrupted, shutting down...")
            rc = 130
        except Exception:
            # Anything else (RuntimeError from _ensure_minimal_cloudimg
            # rejecting an unsupported arch/release combo, KeyError on
            # missing CLI shape, etc.) would otherwise be raised by
            # asyncio.run and traceback'd straight to sys.stderr, which
            # bypasses tee_output and never lands in the per-run log.
            # Route it through print_line so the log captures the same
            # diagnostic the user sees on the terminal.
            import traceback

            print_line(traceback.format_exc().rstrip(), error=True)
            print_line(f"{parsed_args.role}.{parsed_args.machine} crashed", error=True)
            rc = 1
        finally:
            # Emit peak RSS even on failure -- a timed-out run is often the
            # most interesting reading. peak_rss_kb stays 0 when the read
            # failed (cgroup v1 host, qemu died before stop, etc.), in which
            # case we have nothing useful to publish. The sentinel goes
            # through print_line so it lands in both the per-run ANSI log
            # (eyeballable) and the stdout pipe testall.py reads from.
            if m.peak_rss_kb > 0:
                print_line(f"{PEAK_KB_SENTINEL_PREFIX}{m.peak_rss_kb}")
            _print_phase_summary()

    # Drop per-run logs only on a clean pass when the caller (typically
    # testall.py) opted out of keeping them. We wait until tee_output has
    # released the file before unlinking to keep the lifecycle obvious.
    if rc == 0 and not parsed_args.keep_logs:
        m.cleanup_logs()

    return rc


if __name__ == "__main__":
    sys.exit(main())

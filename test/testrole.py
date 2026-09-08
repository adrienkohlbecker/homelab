#!/usr/bin/env -S uv run
"""
Configure and run a single role test with colored output.

Handles argument parsing, environment setup, machine bringup, and log
streaming around an end-to-end converge of one role.
"""

import argparse
import asyncio
import contextlib
import os
import re
import sys
import time
import traceback
from pathlib import Path

from machine import (
    MACHINE_CHOICES,
    PEAK_KB_SENTINEL_PREFIX,
    Machine,
    imagedir_for_host,
    sweep_stale_workdirs,
)
from machine_session import machine_session
from matrix import (
    DEFAULT_UBUNTU,
    UBUNTU_RELEASES,
    RoleTestConfig,
    load_role_test_config,
)
from utils import (
    CommandFailedException,
    IdempotenceFailedException,
    print_line,
    tee_output,
)

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


def parse_args() -> tuple[argparse.Namespace, list[str], RoleTestConfig]:
    """Parse CLI arguments; unknown args are forwarded to Ansible."""
    parser = argparse.ArgumentParser(
        description="Run a single role test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--machine",
        default=None,
        choices=MACHINE_CHOICES,
        help="Machine profile to run against (default: first roles/<role>/meta/test.yml `machines:` key, else 'box')",
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
    parser.add_argument(
        "--workdir-parent",
        type=Path,
        default=os.environ.get("HOMELAB_WORKDIR_PARENT") or None,
        metavar="PATH",
        help="Place the per-run TempDir under this path instead of the imagedir. Lets CI keep the qcow2 tree mounted ro and stage scratch in a container-local /tmp. Falls back to $HOMELAB_WORKDIR_PARENT, then to the imagedir.",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse can leave one or more literal "--" tokens at the head of the
    # remainder depending on positional/optional interleaving; strip them all
    # before forwarding to ansible.
    while pass_args and pass_args[0] == "--":
        pass_args = pass_args[1:]

    # --machine defaults to the role's primary machines: entry. An explicit
    # CLI value still wins, and argparse's choices validate that path before
    # this branch runs.
    role_config = load_role_test_config(args.role)
    if args.machine is None:
        args.machine = next(iter(role_config.machines))
        if args.machine not in MACHINE_CHOICES:
            parser.error(f"roles/{args.role}/meta/test.yml: machine {args.machine!r} not in {sorted(MACHINE_CHOICES)}")

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
    print_line("Verifying idempotence (re-running the role)...")
    async with _phase("idempotence rerun"):
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

    async with machine_session(m, timeout):
        try:
            async with _phase("boot"):
                await m.ensure_booted()
            print_line("Booted")

            async with _phase("ssh wait"):
                await m.ensure_ssh()
            print_line("SSH up")

            # The vanilla cloud image runs cloud-init's config/final stages
            # after sshd comes up, so settle it before changing packages.
            if m.machine == "minimal":
                async with _phase("cloud-init wait"):
                    await m.ensure_cloud_init()

            if not base_prerequisites:
                print_line(f"Skipping base prerequisites: {m.role!r} declares base_prerequisites: false")

            async with _phase("test preparation"):
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
                async with _phase("hook _setup.yml"):
                    await m.ansible_command(site_yml, "-e", "_role_tasks_from=_setup")

            async with _phase("checkmode --check"):
                await m.ansible_command(site_yml, "--check", *pass_args)

            async with _phase("main apply"):
                await m.ansible_command(site_yml, *pass_args)

            await _verify_idempotence(site_yml, m, pass_args)

            # Post-role assertions, if the role declares any.
            if Path(f"roles/{m.role}/tasks/_verify.yml").exists():
                async with _phase("verify.yml"):
                    await m.ansible_command(site_yml, "-e", "_role_tasks_from=_verify")
        except CommandFailedException:
            print_line("Command failed")
            await m.collect_failure_artifacts()
            raise
        except IdempotenceFailedException:
            print_line("Idempotence check failed")
            raise


def main() -> int:
    """CLI entry point for running a single role test."""

    parsed_args, pass_args, role_config = parse_args()

    if parsed_args.benchmark:
        global _BENCHMARK
        _BENCHMARK = True
        # profile_tasks tags every TASK header with elapsed time and prints a
        # TASKS RECAP at end of each play; env var picks it up for every
        # ansible-playbook subprocess without editing ansible.cfg.
        os.environ["ANSIBLE_CALLBACKS_ENABLED"] = "profile_tasks"

    role_main = Path(f"roles/{parsed_args.role}/tasks/main.yml")
    if not role_main.exists():
        print_line(
            f"Error: role '{parsed_args.role}' not found at {role_main}",
            error=True,
        )
        return 1

    # Reap orphaned workdirs from prior SIGKILL'd / OOM'd / power-cut runs
    # before constructing this run's Machine. testall.py also sweeps once
    # before fanning out; the .live-file flock check inside
    # sweep_stale_workdirs keeps parallel workers from racing on each other's
    # freshly-minted workdirs. Scope is imagedir-only, and it only matters for
    # local parallel runs sharing an imagedir.
    sweep_stale_workdirs(imagedir_for_host())

    # Machine.wrapper_timeout layers WRAPPER_GRACE_SECONDS on top of this so
    # the inner `timeout` wrapper outlasts the Python deadline.
    m = Machine(
        machine=parsed_args.machine,
        role=parsed_args.role,
        keep_vm=parsed_args.keep,
        ubuntu_name=parsed_args.ubuntu,
        machine_timeout=parsed_args.timeout,
        upstream_mirrors=parsed_args.upstream_mirrors,
        workdir_parent=parsed_args.workdir_parent,
    )

    rc = 0
    with tee_output(m.output_file):
        try:
            asyncio.run(
                run_test(
                    m,
                    pass_args,
                    base_prerequisites=role_config.base_prerequisites,
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
        except TimeoutError as exc:
            # The outer asyncio.timeout deadline raises a message-less
            # TimeoutError; the phase guards (ensure_booted, ensure_ssh, passt
            # socket, publish-lock) each raise one carrying a specific cause.
            # Surface that cause when present so a slow boot-to-sshd is
            # attributable as such, not misread as the overall per-test timeout.
            if str(exc):
                print_line(str(exc), error=True)
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

    # Clean passes keep the joblog summary and drop noisy per-run artifacts;
    # failures keep everything for post-mortem inspection.
    if rc == 0:
        m.cleanup_logs()

    return rc


if __name__ == "__main__":
    sys.exit(main())

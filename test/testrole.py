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
import shlex
import sys
from pathlib import Path

from build_image import build_image
from machine import (
    DEFAULT_UBUNTU,
    MACHINE_CHOICES,
    Machine,
    PodmanMachine,
    QemuMachine,
    UBUNTU_RELEASES,
    ubuntu_mirrors,
    upsert_memory_row,
)
from utils import CommandFailedException, IdempotenceFailedException, cancel_on_signal, print_line, tee_output

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
        default="container",
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
        "--build-image",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rebuild the homelab:<codename> container image before booting (default: on; container machine only)",
    )
    parser.add_argument(
        "--keep-logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep output/boot/journal logs after a successful run (default: on; --no-keep-logs deletes them, used by testall.py)",
    )
    parser.add_argument("role", help="Role name to test")

    args, pass_args = parser.parse_known_args()

    # argparse can leave one or more literal "--" tokens at the head of the
    # remainder depending on positional/optional interleaving; strip them all
    # to mirror the old shell parsing behavior.
    while pass_args and pass_args[0] == "--":
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

    # Use one shell to avoid repeatedly opening the file. shlex.quote each
    # line so any future $/`/\ in mirror URLs isn't expanded by bash.
    printf_args = " ".join(shlex.quote(line) for line in sources)
    await m.ssh_command("sudo", "bash", "-c", f"printf '%s\\n' {printf_args} > {path}")
    await m.ssh_command("sudo", "apt-get", "update")


_RECAP_CHANGED_RE = re.compile(r"\bchanged=(\d+)")


def _count_changed_tasks(stdout: list[str]) -> int:
    """Sum `changed=N` across every PLAY RECAP host line in the output."""
    return sum(int(m.group(1)) for line in stdout if (m := _RECAP_CHANGED_RE.search(line)))


async def _verify_idempotence(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Re-run the role and fail if any task reports changed."""
    print_line("Verifying idempotence (re-running the role)...")
    result = await m.ansible_command(site_yml, *pass_args)
    changed = _count_changed_tasks(result.stdout)
    if changed > 0:
        raise IdempotenceFailedException(
            f"Role is not idempotent: {changed} task(s) reported changed on the second run"
        )


_STAGE_TAG_RE = re.compile(r"\b_check_stage(\d+)\b")


async def _run_checkmode(site_yml: str, m: Machine, pass_args: list[str]) -> None:
    """Run check mode and staged tags when requested."""
    # Forward pass_args so --list-tags sees the same variable universe as
    # the --check / --tags runs below; otherwise a -e flag that gates a
    # tagged task would make the lists disagree.
    list_tags = await m.ansible_command(site_yml, "--list-tags", *pass_args)

    await m.ansible_command(site_yml, "--check", *pass_args)

    # Some roles split expensive checks into stages; auto-discover every
    # _check_stageN tag and run them in numeric order so adding more stages
    # later doesn't need a code change.
    available_tags = "\n".join(list_tags.stdout)
    stages = sorted({int(n) for n in _STAGE_TAG_RE.findall(available_tags)})
    for n in stages:
        stage = f"_check_stage{n}"
        await m.ansible_command(site_yml, "--tags", stage, *pass_args)
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
                        await m.ensure_booted()
                        print_line("Booted")

                        await m.ensure_ssh()
                        print_line("SSH up")

                        await _configure_apt_sources(m)

                        if m.machine == "minimal":
                            # Fixes systemd-analyze validation error:
                            # /lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' section 'Service', ignoring.
                            await m.ssh_command("sudo", "apt-get", "purge", "--autoremove", "--yes", "snapd")

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
                        print_line("Command failed")
                        # Best-effort: a journal-collection failure must not
                        # shadow the underlying CommandFailedException.
                        with contextlib.suppress(Exception):
                            await m.collect_journal()
                            m.print_journal_tail()
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

    role_main = Path(f"roles/{parsed_args.role}/tasks/main.yml")
    if not role_main.exists():
        print_line(
            f"Error: role '{parsed_args.role}' not found at {role_main}",
            stderr=True,
        )
        return 1

    machine_cls = PodmanMachine if parsed_args.machine == "container" else QemuMachine
    # Inner timeout/podman --timeout is a last-resort cleanup if Python dies;
    # it must outlast the Python deadline so testrole's own timer fires first
    # and we get a clean rc=124 + stop(). 60s grace covers normal teardown.
    m: Machine = machine_cls(
        machine=parsed_args.machine,
        role=parsed_args.role,
        keep_vm=parsed_args.keep,
        ubuntu_name=parsed_args.ubuntu,
        machine_timeout=parsed_args.timeout + 60,
    )

    rc = 0
    with tee_output(m.output_file):
        if parsed_args.build_image and parsed_args.machine == "container":
            try:
                rc = build_image(parsed_args.ubuntu)
            except KeyboardInterrupt:
                # build_image() runs its own asyncio.run(); SIGINT during a
                # build raises KeyboardInterrupt out of it. Translate into
                # the same exit code as a cancelled test run.
                print_line("\nInterrupted during image build, shutting down...", stderr=True)
                return 130
            if rc != 0:
                print_line(
                    f"{parsed_args.role}.{parsed_args.machine} build failed",
                    stderr=True,
                )
                return rc

        try:
            asyncio.run(run_test(
                m,
                pass_args,
                checkmode=parsed_args.checkmode,
                idempotence=parsed_args.idempotence,
                timeout=parsed_args.timeout,
            ))
        except CommandFailedException as exc:
            print_line(str(exc), stderr=True)
            print_line(f"{parsed_args.role}.{parsed_args.machine} failed", stderr=True)
            rc = 1
        except IdempotenceFailedException as exc:
            print_line(str(exc), stderr=True)
            print_line(f"{parsed_args.role}.{parsed_args.machine} not idempotent", stderr=True)
            rc = 125
        except TimeoutError:
            print_line(
                f"{parsed_args.role}.{parsed_args.machine} timed out after {parsed_args.timeout}s",
                stderr=True,
            )
            rc = 124  # GNU `timeout`'s exit code for "command timed out"
        except asyncio.CancelledError:
            print_line("\nInterrupted, shutting down...")
            rc = 130
        finally:
            # Record peak RSS for QEMU runs even on failure -- a timed-out
            # run is often the most interesting reading. peak_rss_kb stays
            # 0 if the sampler never got going (e.g., boot failed before
            # ensure_booted), in which case we have nothing useful to log.
            if parsed_args.machine != "container" and m.peak_rss_kb > 0:
                upsert_memory_row(
                    parsed_args.role, parsed_args.ubuntu, parsed_args.machine, m.peak_rss_kb,
                )

    # Drop per-run logs only on a clean pass when the caller (typically
    # testall.py) opted out of keeping them. We wait until tee_output has
    # released the file before unlinking to keep the lifecycle obvious.
    if rc == 0 and not parsed_args.keep_logs:
        m.cleanup_logs()

    return rc


if __name__ == "__main__":
    sys.exit(main())

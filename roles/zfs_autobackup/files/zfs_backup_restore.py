#!/usr/bin/env python3
"""Restore an explicit snapshot range from a pull replica onto a rebuilt host.

The restore protocol deliberately treats every dataset independently.
The target tree must not exist: an interrupted restore is discarded before the
command is retried, keeping target inspection and recovery explicit.

Pull replicas override readonly, canmount, and mountpoint locally. ``zfs send
-bp`` sends the received source properties rather than those holder-local
overrides. The target becomes writable and mountable only after every dataset
has reached END_SUFFIX and its endpoint GUID has been verified.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NoReturn, TextIO

USAGE = """Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET START_SUFFIX END_SUFFIX TARGET_DATASET MOUNTPOINT
Example: zfs_backup_restore ak@lab apoc/lab/rpool/services \\
  bak-20260801020000 bak-20260813020000 rpool/services /mnt/services"""
SSH_DESTINATION = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9_.:-]+", re.ASCII)
ZFS_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", re.ASCII)
SNAPSHOT_SUFFIX = re.compile(r"bak-[0-9]{14}", re.ASCII)
MOUNTPOINT = re.compile(r"/[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*", re.ASCII)
SSH_BULK_OPTIONS = (
    "-o",
    "ControlPath=none",
    "-o",
    "Compression=no",
    "-o",
    "Ciphers=^aes128-gcm@openssh.com",
)


class RestoreError(Exception):
    """A safety check or external command prevented the restore."""


@dataclass(frozen=True)
class Config:
    """Validated command-line configuration for one restore."""

    target_ssh: str
    replica_dataset: str
    start_suffix: str
    end_suffix: str
    target_dataset: str
    mountpoint: str


@dataclass(frozen=True)
class DatasetPlan:
    """Selected source history and its destination dataset."""

    source: str
    target: str
    snapshots: list[str]


def fail(message: str, exit_code: int = 1) -> NoReturn:
    """Print an operator-facing error and terminate with ``exit_code``."""

    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def command_text(command: list[str] | tuple[str, ...]) -> str:
    """Render an argument vector for diagnostics without executing it."""

    return shlex.join(command)


def capture(
    command: list[str] | tuple[str, ...],
    *,
    check: bool = True,
    stderr: int | TextIO | None = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    """Run a text command and capture stdout, raising on failure by default."""

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
    except OSError as error:
        raise RestoreError(f"could not run command {command_text(command)}: {error}") from error
    if check and result.returncode:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        if detail:
            print(detail, file=sys.stderr)
        raise RestoreError(f"command failed with exit {result.returncode}: {command_text(command)}")
    return result


def run(command: list[str] | tuple[str, ...]) -> None:
    """Run a command with inherited stdio and raise if it fails."""

    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        raise RestoreError(f"could not run command {command_text(command)}: {error}") from error
    if result.returncode:
        raise RestoreError(f"command failed with exit {result.returncode}: {command_text(command)}")


def lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Return the non-empty stdout lines from a completed text command."""

    return [line for line in result.stdout.splitlines() if line]


def ssh_command(config: Config, *command: str) -> list[str]:
    """Build a non-interactive SSH command for a short remote operation."""

    # Short probes never consume stdin. -n prevents SSH from stealing the
    # confirmation prompt or a caller's input; streaming receives omit it.
    return ["ssh", "-n", config.target_ssh, *command]


def zfs_command(*args: str, sudo: bool = False) -> list[str]:
    """Build a local ZFS command, optionally elevated through sudo."""

    return ["sudo", "zfs", *args] if sudo else ["zfs", *args]


def zfs_capture(
    *args: str,
    sudo: bool = False,
    check: bool = True,
    stderr: int | TextIO | None = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    """Run a local ZFS command and capture its text output."""

    return capture(zfs_command(*args, sudo=sudo), check=check, stderr=stderr)


def remote_zfs_capture(config: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an elevated ZFS command remotely and capture its text output."""

    return capture(ssh_command(config, "sudo", "zfs", *args), check=check)


def remote_zfs_run(config: Config, *args: str) -> None:
    """Run an elevated remote ZFS command with inherited stdio."""

    run(ssh_command(config, "sudo", "zfs", *args))


def parse_config(argv: list[str]) -> Config:
    """Parse and validate the six positional command-line arguments."""

    if len(argv) != 6:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    config = Config(*argv)
    # OpenSSH joins remote argv into a shell command instead of transmitting an
    # argv vector. Restrict every interpolated value to a shell-safe alphabet.
    if not SSH_DESTINATION.fullmatch(config.target_ssh):
        fail(f"invalid SSH destination: {config.target_ssh}", 2)
    for label, name in (
        ("replica dataset", config.replica_dataset),
        ("target dataset", config.target_dataset),
    ):
        if not ZFS_NAME.fullmatch(name):
            fail(f"unsupported {label} name: {name}", 2)
    mountpoint = PurePosixPath(config.mountpoint)
    if ".." in mountpoint.parts or str(mountpoint) != config.mountpoint or not MOUNTPOINT.fullmatch(config.mountpoint):
        fail(f"unsupported target mountpoint: {config.mountpoint}", 2)
    if not SNAPSHOT_SUFFIX.fullmatch(config.start_suffix) or not SNAPSHOT_SUFFIX.fullmatch(config.end_suffix):
        fail("snapshot suffixes must match bak-YYYYMMDDhhmmss", 2)
    return config


def selected_snapshots(config: Config, dataset: str) -> list[str]:
    """Return the inclusive source snapshot range in creation order."""

    # Depth one excludes snapshots belonging to descendant datasets, which
    # receive their own dataset streams.
    result = zfs_capture("list", "-H", "-d", "1", "-t", "snapshot", "-o", "name", "-s", "createtxg", dataset)
    prefix = f"{dataset}@"
    suffixes = [name.removeprefix(prefix) for name in lines(result) if name.startswith(prefix)]
    if any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", suffix, re.ASCII) for suffix in suffixes):
        raise RestoreError(f"unsupported snapshot name on {dataset}")
    try:
        start = suffixes.index(config.start_suffix)
        end = suffixes.index(config.end_suffix)
    except ValueError as error:
        raise RestoreError(f"snapshot range is incomplete on {dataset}") from error
    if start > end:
        raise RestoreError(f"starting snapshot is newer than target snapshot on {dataset}")
    return suffixes[start : end + 1]


def build_plans(config: Config) -> list[DatasetPlan]:
    """Map every replica dataset to its target and selected snapshot history."""

    try:
        datasets = lines(zfs_capture("list", "-r", "-H", "-o", "name", config.replica_dataset))
    except RestoreError as error:
        raise RestoreError(f"replica dataset does not exist: {config.replica_dataset}") from error

    plans = []
    for dataset in datasets:
        if not ZFS_NAME.fullmatch(dataset):
            raise RestoreError(f"unsupported dataset name: {dataset}")
        # A clone depends on its origin snapshot. Reconstructing that graph
        # cannot be expressed by this protocol's independent dataset streams.
        if zfs_capture("get", "-H", "-o", "value", "origin", dataset).stdout.strip() != "-":
            raise RestoreError(f"clone datasets are not supported: {dataset}")
        target = config.target_dataset + dataset.removeprefix(config.replica_dataset)
        plans.append(DatasetPlan(dataset, target, selected_snapshots(config, dataset)))
    return plans


def snapshot_guid(dataset: str, suffix: str) -> str:
    """Return the stable ZFS identity of a local snapshot."""

    return zfs_capture("get", "-H", "-p", "-o", "value", "guid", f"{dataset}@{suffix}").stdout.strip()


def inspect_targets(config: Config) -> None:
    """Require the destination tree to be absent before any stream starts."""

    remote_datasets = set(lines(remote_zfs_capture(config, "list", "-H", "-o", "name")))
    target_prefix = f"{config.target_dataset}/"
    existing = sorted(
        dataset for dataset in remote_datasets if dataset == config.target_dataset or dataset.startswith(target_prefix)
    )
    if existing:
        raise RestoreError(
            f"{config.target_dataset} already exists on {config.target_ssh}; "
            "remove the incomplete target tree before restoring"
        )


def receive(config: Config, send_command: list[str], target: str) -> None:
    """Stream one ZFS send through mbuffer into an unmounted remote receive."""

    commands = [
        send_command,
        ["mbuffer", "-m", "256M"],
        [
            "ssh",
            # Bulk streams must consume stdin. They also bypass multiplexing and
            # SSH compression so mbuffer reflects the actual bottleneck.
            *SSH_BULK_OPTIONS,
            config.target_ssh,
            "sudo",
            "zfs",
            "recv",
            "-u",
            target,
        ],
    ]
    processes: list[subprocess.Popen[bytes]] = []
    previous_stdout = None
    for index, command in enumerate(commands):
        try:
            process = subprocess.Popen(
                command,
                stdin=previous_stdout,
                stdout=subprocess.PIPE if index < len(commands) - 1 else None,
            )
        except OSError as error:
            if previous_stdout is not None:
                previous_stdout.close()
            for started_process in processes:
                started_process.terminate()
            for started_process in processes:
                started_process.wait()
            raise RestoreError(f"could not start restore pipeline command {command_text(command)}: {error}") from error
        if previous_stdout is not None:
            # The parent must release its duplicate read end. Otherwise an
            # upstream writer may never observe SIGPIPE when downstream fails.
            previous_stdout.close()
        previous_stdout = process.stdout
        processes.append(process)

    return_codes = [process.wait() for process in processes]
    failures = [
        f"{command_text(command)} (exit {return_code})"
        for command, return_code in zip(commands, return_codes, strict=True)
        if return_code
    ]
    if failures:
        raise RestoreError(f"restore pipeline failed: {'; '.join(failures)}")


def sync_plan(config: Config, plan: DatasetPlan) -> None:
    """Send every selected snapshot to a new destination dataset."""

    for index, suffix in enumerate(plan.snapshots):
        send_command = zfs_command("send", "-bpcv", sudo=True)
        if index:
            send_command.extend(("-i", f"@{plan.snapshots[index - 1]}"))
        send_command.append(f"{plan.source}@{suffix}")
        print(f"Sending {plan.source}@{suffix} -> {plan.target}")
        receive(config, send_command, plan.target)


def verify_endpoints(config: Config, plans: list[DatasetPlan]) -> None:
    """Require the GUID-matching endpoint snapshot on every target dataset."""

    for plan in plans:
        expected = snapshot_guid(plan.source, config.end_suffix)
        result = remote_zfs_capture(
            config,
            "get",
            "-H",
            "-p",
            "-o",
            "value",
            "guid",
            f"{plan.target}@{config.end_suffix}",
            check=False,
        )
        if result.stdout.strip() != expected:
            raise RestoreError(f"restore incomplete: {plan.target}@{config.end_suffix} is missing")


def finalize(config: Config, plans: list[DatasetPlan]) -> None:
    """Make a verified target writable, mountable, and locally tagged."""

    remote_zfs_run(
        config,
        "set",
        "readonly=off",
        "canmount=on",
        f"mountpoint={config.mountpoint}",
        config.target_dataset,
    )

    for plan in plans:
        properties = dict(
            line.split("\t", 1)
            for line in lines(
                remote_zfs_capture(
                    config,
                    "get",
                    "-H",
                    "-o",
                    "property,value",
                    "mountpoint,canmount,mounted,autobackup:bak",
                    plan.target,
                )
            )
        )
        # A property-based mount check avoids invalid explicit mounts for
        # datasets whose received configuration uses none, legacy, or off.
        if (
            properties["mountpoint"] not in {"none", "legacy"}
            and properties["canmount"] != "off"
            and properties["mounted"] == "no"
        ):
            remote_zfs_run(config, "mount", plan.target)

        # Received-source tags are invisible to the local snapshot picker.
        if properties["autobackup:bak"] == "true":
            remote_zfs_run(config, "set", "autobackup:bak=true", plan.target)


def main(argv: list[str]) -> None:
    """Drive confirmation, validation, transfer, and finalization."""

    config = parse_config(argv)
    plans = build_plans(config)

    print()
    print(
        f"Restoring {config.replica_dataset}@{config.start_suffix}..{config.end_suffix} "
        f"-> {config.target_ssh} as {config.target_dataset}"
    )
    try:
        reply = input("Proceed? [y/N] ")
    except EOFError:
        reply = ""
    if reply != "y":
        raise RestoreError("Aborted")

    # Target inspection starts only after confirmation, immediately before
    # transfer. zfs recv without -F remains the race guard if state changes
    # between a probe and its stream.
    inspect_targets(config)
    for plan in plans:
        sync_plan(config, plan)
    verify_endpoints(config, plans)
    finalize(config, plans)
    print("Restore complete. Converge the host next -- it re-asserts the remaining dataset properties.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RestoreError as error:
        fail(str(error))

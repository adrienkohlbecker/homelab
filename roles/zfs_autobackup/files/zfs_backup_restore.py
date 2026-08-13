#!/usr/bin/env python3
"""Restore an explicit snapshot range from a pull replica onto a rebuilt host.

The restore protocol deliberately uses one stream per dataset and snapshot.
That makes every completed snapshot a durable checkpoint: a later invocation
can validate the target as an exact GUID-matching prefix and continue from the
next snapshot. A stream interrupted between checkpoints is resumed with its
ZFS receive token.

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

USAGE = "Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET START_SUFFIX " "END_SUFFIX TARGET_DATASET MOUNTPOINT"
SSH_DESTINATION = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9_.:-]+", re.ASCII)
ZFS_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", re.ASCII)
SNAPSHOT_SUFFIX = re.compile(r"bak-[0-9]{14}", re.ASCII)
RESUME_TOKEN = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
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
    target_ssh: str
    replica_dataset: str
    start_suffix: str
    end_suffix: str
    target_dataset: str
    mountpoint: str


@dataclass
class DatasetPlan:
    """Selected source history and the validated progress of its target."""

    source: str
    target: str
    snapshots: list[str]
    received_count: int = 0
    resume_token: str | None = None

    @property
    def complete(self) -> bool:
        return self.received_count == len(self.snapshots) and self.resume_token is None


def fail(message: str, exit_code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def command_text(command: list[str] | tuple[str, ...]) -> str:
    return shlex.join(command)


def capture(
    command: list[str] | tuple[str, ...],
    *,
    check: bool = True,
    stderr: int | TextIO | None = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        if detail:
            print(detail, file=sys.stderr)
        raise RestoreError(f"command failed with exit {result.returncode}: {command_text(command)}")
    return result


def run(command: list[str] | tuple[str, ...]) -> None:
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RestoreError(f"command failed with exit {result.returncode}: {command_text(command)}")


def trace(command: list[str]) -> None:
    print(f"$ {command_text(command)}")
    run(command)


def lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line]


def ssh_command(config: Config, *command: str) -> list[str]:
    return ["ssh", "-n", config.target_ssh, *command]


def zfs_command(*args: str, sudo: bool = False) -> list[str]:
    return ["sudo", "zfs", *args] if sudo else ["zfs", *args]


def zfs_capture(
    *args: str,
    sudo: bool = False,
    check: bool = True,
    stderr: int | TextIO | None = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    return capture(zfs_command(*args, sudo=sudo), check=check, stderr=stderr)


def zfs_value(*args: str) -> str:
    return zfs_capture(*args).stdout.strip()


def remote_zfs_capture(config: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return capture(ssh_command(config, "sudo", "zfs", *args), check=check)


def remote_zfs_value(config: Config, *args: str) -> str:
    return remote_zfs_capture(config, *args).stdout.strip()


def remote_zfs_run(config: Config, *args: str) -> None:
    run(ssh_command(config, "sudo", "zfs", *args))


def parse_config(argv: list[str]) -> Config:
    if len(argv) != 6:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    config = Config(*argv)
    if not SSH_DESTINATION.fullmatch(config.target_ssh) or config.target_ssh.startswith("-"):
        fail(f"invalid SSH destination: {config.target_ssh}", 2)
    for label, name in (
        ("replica dataset", config.replica_dataset),
        ("target dataset", config.target_dataset),
    ):
        if not ZFS_NAME.fullmatch(name):
            fail(f"unsupported {label} name: {name}", 2)
    if (
        not config.mountpoint.startswith("/")
        or ".." in PurePosixPath(config.mountpoint).parts
        or not re.fullmatch(r"/[A-Za-z0-9_.:/-]+", config.mountpoint, re.ASCII)
    ):
        fail(f"unsupported target mountpoint: {config.mountpoint}", 2)
    if not SNAPSHOT_SUFFIX.fullmatch(config.start_suffix) or not SNAPSHOT_SUFFIX.fullmatch(config.end_suffix):
        fail("snapshot suffixes must match bak-YYYYMMDDhhmmss", 2)
    return config


def selected_snapshots(config: Config, dataset: str) -> list[str]:
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
    try:
        datasets = lines(zfs_capture("list", "-r", "-H", "-o", "name", config.replica_dataset))
    except RestoreError as error:
        raise RestoreError(f"replica dataset does not exist: {config.replica_dataset}") from error

    plans = []
    for dataset in datasets:
        if not ZFS_NAME.fullmatch(dataset):
            raise RestoreError(f"unsupported dataset name: {dataset}")
        if zfs_value("get", "-H", "-o", "value", "origin", dataset) != "-":
            raise RestoreError(f"clone datasets are not supported: {dataset}")
        target = config.target_dataset + dataset.removeprefix(config.replica_dataset)
        plans.append(DatasetPlan(dataset, target, selected_snapshots(config, dataset)))
    return plans


def preview(config: Config, plans: list[DatasetPlan]) -> None:
    for plan in plans:
        trace(zfs_command("send", "-nPbpvc", f"{plan.source}@{config.start_suffix}", sudo=True))
        if config.start_suffix != config.end_suffix:
            trace(
                zfs_command(
                    "send",
                    "-nPbpvc",
                    "-I",
                    f"@{config.start_suffix}",
                    f"{plan.source}@{config.end_suffix}",
                    sudo=True,
                )
            )


def snapshot_guid(dataset: str, suffix: str) -> str:
    return zfs_value("get", "-H", "-p", "-o", "value", "guid", f"{dataset}@{suffix}")


def remote_snapshot_rows(config: Config, target: str) -> list[tuple[str, str]]:
    result = remote_zfs_capture(
        config,
        "list",
        "-H",
        "-d",
        "1",
        "-t",
        "snapshot",
        "-o",
        "name",
        "-s",
        "createtxg",
        target,
        check=False,
    )
    prefix = f"{target}@"
    rows = []
    for snapshot in (name for name in lines(result) if name.startswith(prefix)):
        guid = remote_zfs_value(config, "get", "-H", "-p", "-o", "value", "guid", snapshot)
        rows.append((snapshot.removeprefix(prefix), guid))
    return rows


def token_suffix(token: str) -> str:
    result = zfs_capture(
        "send",
        "-nP",
        "-t",
        token,
        sudo=True,
        stderr=subprocess.STDOUT,
    )
    matches = re.findall(r"toname = .*@([^\s]+)", result.stdout)
    if not matches:
        raise RestoreError("receive resume token preview did not identify a snapshot")
    return matches[-1]


def inspect_targets(config: Config, plans: list[DatasetPlan]) -> None:
    remote_datasets = set(lines(remote_zfs_capture(config, "list", "-H", "-o", "name")))
    expected_targets = {plan.target for plan in plans}
    target_prefix = f"{config.target_dataset}/"
    unexpected = sorted(
        dataset
        for dataset in remote_datasets
        if (dataset == config.target_dataset or dataset.startswith(target_prefix)) and dataset not in expected_targets
    )
    if unexpected:
        raise RestoreError(f"unexpected dataset below {config.target_dataset} on {config.target_ssh}: {unexpected[0]}")

    for plan in plans:
        if plan.target not in remote_datasets:
            continue
        token = remote_zfs_value(
            config,
            "get",
            "-H",
            "-o",
            "value",
            "receive_resume_token",
            plan.target,
        )
        rows = remote_snapshot_rows(config, plan.target)
        for index, (suffix, remote_guid) in enumerate(rows):
            if index >= len(plan.snapshots) or suffix != plan.snapshots[index]:
                raise RestoreError(f"{plan.target} is not a prefix of the requested snapshot range")
            if remote_guid != snapshot_guid(plan.source, suffix):
                raise RestoreError(f"snapshot GUID mismatch for {plan.target}@{suffix}")
        plan.received_count = len(rows)

        if token == "-":
            if not rows:
                raise RestoreError(f"{plan.target} exists without requested snapshots or a resume token")
            continue
        if not RESUME_TOKEN.fullmatch(token) or plan.received_count >= len(plan.snapshots):
            raise RestoreError(f"invalid receive resume state on {plan.target}")
        if token_suffix(token) != plan.snapshots[plan.received_count]:
            raise RestoreError(f"receive resume token on {plan.target} is outside the requested range")
        plan.resume_token = token

    if sum(plan.resume_token is not None for plan in plans) > 1:
        raise RestoreError(f"multiple receive resume tokens exist below {config.target_dataset}")
    if all(plan.complete for plan in plans):
        raise RestoreError(
            f"{config.target_dataset} already contains the requested snapshot range -- refusing to overwrite it"
        )


def pipe_commands(commands: list[list[str]]) -> None:
    """Run a binary stream pipeline and fail if any member fails."""

    processes: list[subprocess.Popen[bytes]] = []
    previous_stdout = None
    for index, command in enumerate(commands):
        process = subprocess.Popen(
            command,
            stdin=previous_stdout,
            stdout=subprocess.PIPE if index < len(commands) - 1 else None,
        )
        if previous_stdout is not None:
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


def receive(config: Config, send_command: list[str], target: str) -> None:
    pipe_commands(
        [
            send_command,
            ["mbuffer", "-m", "256M"],
            [
                "ssh",
                *SSH_BULK_OPTIONS,
                config.target_ssh,
                "sudo",
                "zfs",
                "recv",
                "-su",
                target,
            ],
        ]
    )


def sync_plan(config: Config, plan: DatasetPlan) -> None:
    index = plan.received_count
    if plan.resume_token:
        print(f"Resuming an interrupted restore into {plan.target}")
        trace(zfs_command("send", "-nP", "-t", plan.resume_token, sudo=True))
        receive(config, zfs_command("send", "-t", plan.resume_token, sudo=True), plan.target)
        index += 1

    while index < len(plan.snapshots):
        suffix = plan.snapshots[index]
        send_command = zfs_command("send", "-bpcv", sudo=True)
        if index:
            send_command.extend(("-i", f"@{plan.snapshots[index - 1]}"))
        send_command.append(f"{plan.source}@{suffix}")
        print(f"Sending {plan.source}@{suffix} -> {plan.target}")
        receive(config, send_command, plan.target)
        index += 1


def verify_endpoints(config: Config, plans: list[DatasetPlan]) -> None:
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
                    "mountpoint,canmount,mounted",
                    plan.target,
                )
            )
        )
        if (
            properties["mountpoint"] not in {"none", "legacy"}
            and properties["canmount"] != "off"
            and properties["mounted"] == "no"
        ):
            remote_zfs_run(config, "mount", plan.target)

        # Received-source tags are invisible to the local snapshot picker.
        if (
            remote_zfs_value(
                config,
                "get",
                "-H",
                "-o",
                "value",
                "autobackup:bak",
                plan.target,
            )
            == "true"
        ):
            remote_zfs_run(config, "set", "autobackup:bak=true", plan.target)

    print()
    remote_zfs_run(
        config,
        "list",
        "-r",
        "-o",
        "name,mountpoint,mounted,readonly",
        config.target_dataset,
    )
    remote_zfs_run(
        config,
        "get",
        "-t",
        "filesystem",
        "-r",
        "-o",
        "name,value,source",
        "autobackup:bak",
        config.target_dataset,
    )


def main(argv: list[str]) -> None:
    config = parse_config(argv)
    plans = build_plans(config)
    preview(config, plans)

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

    # The target is inspected exactly once, immediately before transfer. ZFS
    # recv without -F remains the race guard if it changes after inspection.
    inspect_targets(config, plans)
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

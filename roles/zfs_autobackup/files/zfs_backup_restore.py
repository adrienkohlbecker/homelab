#!/usr/bin/env python3
"""Restore an explicit snapshot range from a pull replica onto a rebuilt host.

The restore protocol deliberately treats every dataset independently, using
one full stream followed by one aggregate incremental stream when needed.
The target tree must not exist: an interrupted restore is discarded before the
command is retried, keeping target inspection and recovery explicit. Receives
still pass ``-s``, so an interrupted stream leaves a resume token behind. This
program never continues one -- the absent-target rule is what keeps a restore
auditable -- but the token lets an operator finish a stalled multi-hour
transfer by hand rather than resend it from zero.
Clone relationships are not recreated; clone datasets restore as independent
filesystems.

Pull replicas override readonly, canmount, and mountpoint locally. ``zfs send
-bp`` sends the received source properties rather than those holder-local
overrides -- which for this fleet means ``readonly=off``, ``canmount=on`` and
the source's absolute mountpoint. Receiving those verbatim would leave a
half-transferred tree that mounts itself over live paths on the next boot, so
every receive pins ``readonly=on canmount=noauto mountpoint=none`` locally and
``finalize`` clears those pins only once the whole tree has landed. A received
mountpoint pointing outside MOUNTPOINT is re-anchored inside it rather than
mounted where the source had it.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

EXAMPLE = """example:
  zfs_backup_restore ak@lab apoc/lab/rpool/services \\
    bak-20260801020000 bak-20260813020000 rpool/services /mnt/services"""
# Bulk streams bypass multiplexing and SSH compression so mbuffer reflects the
# actual bottleneck. A restore runs for hours over WireGuard to a freshly
# rebuilt host: without keepalives a silently dropped path leaves ssh blocked in
# read() and the pipeline waiting forever; 30s x 6 declares the peer dead in
# three minutes and surfaces a real exit code instead.
SSH_BULK_OPTIONS = [
    *("-o", "ControlPath=none", "-o", "Compression=no", "-o", "Ciphers=^aes128-gcm@openssh.com"),
    *("-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"),
]
# Values ZFS reports for datasets that have no mountpoint of their own.
UNMOUNTABLE = frozenset({"none", "legacy", "-"})


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


def run(command: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its text output unless ``capture`` is false.

    Raises RestoreError when the command cannot start or, with ``check``,
    exits nonzero; captured stderr is echoed first so the cause is visible.
    """

    try:
        result = subprocess.run(command, check=False, capture_output=capture, text=True)
    except OSError as error:
        raise RestoreError(f"could not run command {shlex.join(command)}: {error}") from error
    if check and result.returncode:
        if capture and result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise RestoreError(f"command failed with exit {result.returncode}: {shlex.join(command)}")
    return result


def remote(config: Config, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` on the target and capture its output.

    OpenSSH sends its command as one string that the remote shell re-parses,
    so the argv is quoted here instead of letting ssh join raw words. -n keeps
    ssh from stealing the confirmation prompt or a caller's input.
    """

    return run(["ssh", "-n", config.target_ssh, shlex.join(argv)], check=check)


def remote_zfs(config: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an elevated ZFS command on the target."""

    return remote(config, "sudo", "-n", "zfs", *args, check=check)


def lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Return the non-empty stdout lines from a completed text command."""

    return [line for line in result.stdout.splitlines() if line]


def parse_config(argv: list[str]) -> Config:
    """Parse the six positional command-line arguments."""

    parser = argparse.ArgumentParser(
        prog="zfs_backup_restore",
        description=__doc__,
        epilog=EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target_ssh", help="user@host of the rebuilt target")
    parser.add_argument("replica_dataset", help="root of the replica tree to send")
    parser.add_argument("start_suffix", help="oldest snapshot to retain (bak-YYYYMMDDhhmmss)")
    parser.add_argument("end_suffix", help="newest snapshot to restore (bak-YYYYMMDDhhmmss)")
    parser.add_argument("target_dataset", help="root of the destination tree, which must not exist")
    parser.add_argument("mountpoint", help="mountpoint for the restored tree")

    config = Config(**vars(parser.parse_args(argv)))
    # bak-YYYYMMDDhhmmss names sort chronologically.
    if config.start_suffix > config.end_suffix:
        parser.error("starting snapshot is newer than ending snapshot")
    # desired_mountpoint decides containment lexically, so the root must be a
    # normalized absolute path below /.
    path = PurePosixPath(config.mountpoint)
    if not path.is_absolute() or str(path) != config.mountpoint or ".." in path.parts or path.parent == path:
        parser.error(f"target mountpoint must be a normalized absolute path below /: {config.mountpoint}")
    return config


def resolve_sources(config: Config) -> list[str]:
    """Map the replica tree and require both range endpoints on every dataset."""

    try:
        sources = lines(
            run(["zfs", "list", "-r", "-H", "-t", "filesystem,volume", "-o", "name", config.replica_dataset])
        )
    except RestoreError as error:
        raise RestoreError(f"replica dataset does not exist: {config.replica_dataset}") from error

    suffixes = dict.fromkeys((config.start_suffix, config.end_suffix))
    endpoints = [f"{source}@{suffix}" for source in sources for suffix in suffixes]
    try:
        run(["zfs", "list", "-H", "-t", "snapshot", "-o", "name", *endpoints])
    except RestoreError as error:
        raise RestoreError("snapshot range is incomplete on the replica tree") from error
    return sources


def check_target_preflight(config: Config) -> None:
    """Require the target host to be able to run its half of the pipeline.

    The target is a freshly reinstalled host that has not been converged yet,
    so neither mbuffer nor passwordless sudo is in place. Both are needed by
    the receive leg, and both fail obscurely mid-stream: mbuffer as a broken
    remote pipe, sudo by reading the binary send stream as password attempts.
    """

    mbuffer = remote(config, "command", "-v", "mbuffer", check=False)
    if mbuffer.returncode == 255:
        raise RestoreError(f"cannot reach {config.target_ssh}: {mbuffer.stderr.strip()}")
    if mbuffer.returncode:
        raise RestoreError(
            f"mbuffer is missing on {config.target_ssh}; the receive leg buffers there too. "
            "Install it first: sudo apt-get install -y mbuffer"
        )

    if remote_zfs(config, "--version", check=False).returncode:
        raise RestoreError(
            f"passwordless sudo zfs is unavailable on {config.target_ssh}; "
            "the receive leg cannot answer a password prompt mid-stream"
        )


def inspect_targets(config: Config) -> None:
    """Require the destination tree to be absent before any stream starts."""

    result = remote_zfs(
        config, "list", "-r", "-H", "-t", "filesystem,volume", "-o", "name", config.target_dataset, check=False
    )
    if result.returncode:
        # zfs list exits 1 for a dataset that does not exist -- the state this
        # check requires. Any other code (notably ssh's 255) is a real error.
        if result.returncode != 1:
            raise RestoreError(
                f"could not inspect {config.target_dataset} on {config.target_ssh}: {result.stderr.strip()}"
            )
        return

    recovery = "\n".join(f"  sudo zfs receive -A {shlex.quote(dataset)}" for dataset in lines(result))
    raise RestoreError(
        f"{config.target_dataset} already exists on {config.target_ssh}; "
        f"remove the incomplete target tree before restoring. On {config.target_ssh}:\n"
        f"{recovery}\n"
        f"  sudo zfs destroy -r {shlex.quote(config.target_dataset)}"
    )


def receive(config: Config, send_command: list[str], target: str) -> None:
    """Stream one ZFS send through mbuffer into an unmounted remote receive."""

    # mbuffer on both ends: the local buffer absorbs zfs send burstiness, the
    # remote one keeps the ssh pipe draining while zfs recv stalls on txg
    # syncs (-q: no stats on the non-tty side). ssh transmits its command as
    # one string, so the remote pipe is parsed on the target.
    #
    # recv -s leaves a resume token on an interrupted stream, so an aborted
    # multi-hour transfer can be continued by hand rather than restarted; the
    # -o pins keep a half-received tree inert until finalize releases it.
    pins = ["-o", "readonly=on", "-o", "canmount=noauto", "-o", "mountpoint=none"]
    remote_receive = "mbuffer -q -m 256M | " + shlex.join(["sudo", "-n", "zfs", "recv", "-s", "-u", *pins, target])
    # Bulk streams must consume stdin, so ssh runs without -n here.
    ssh = ["ssh", *SSH_BULK_OPTIONS, config.target_ssh, remote_receive]
    pipeline = " | ".join(shlex.join(command) for command in (send_command, ["mbuffer", "-m", "256M"], ssh))
    # Echo the real invocation, matching the sibling shell scripts:
    # rerunning one dataset by hand is the documented fallback mid-restore.
    print(f"$ {pipeline}")
    run(["bash", "-o", "pipefail", "-c", pipeline], capture=False)


def sync_dataset(config: Config, source: str) -> None:
    """Send the selected history to a new destination dataset."""

    target = config.target_dataset + source.removeprefix(config.replica_dataset)
    print(f"Sending {source}@{config.start_suffix} -> {target}")
    receive(config, ["sudo", "zfs", "send", "-bpcveL", f"{source}@{config.start_suffix}"], target)

    if config.start_suffix == config.end_suffix:
        return

    print(f"Sending {source}@{config.start_suffix}..{config.end_suffix} -> {target}")
    receive(
        config,
        ["sudo", "zfs", "send", "-bpcveL", "-I", f"@{config.start_suffix}", f"{source}@{config.end_suffix}"],
        target,
    )


def remote_properties(config: Config) -> dict[str, dict[str, str]]:
    """Read the finalization properties for the whole restored tree at once.

    Each dataset maps property name to its effective value. The received
    mountpoint is retained separately because receive pins temporarily hide it.
    """

    # -t filesystem,volume: zfs get recurses into snapshots by default, which
    # would swamp the result with rows carrying none of these properties.
    result = remote_zfs(
        config,
        *("get", "-r", "-H", "-t", "filesystem,volume", "-o", "name,property,value,received"),
        *("type,mountpoint,canmount,mounted,autobackup:bak", config.target_dataset),
    )
    properties: dict[str, dict[str, str]] = {}
    for line in lines(result):
        name, property_name, value, received = line.split("\t", 3)
        values = properties.setdefault(name, {})
        values[property_name] = value
        if property_name == "mountpoint":
            values["mountpoint_received"] = received
    return properties


def desired_mountpoint(config: Config, dataset: str, values: dict[str, str]) -> str:
    """Choose a mountpoint inside MOUNTPOINT for one restored dataset.

    ``zfs send -b`` replays the source's absolute mountpoint. Honouring it
    would mount the restored copy over the live directory of the same name on
    the target host, so anything outside the restore root -- or absent from the
    stream entirely -- is re-anchored at its position under MOUNTPOINT.
    """

    if dataset == config.target_dataset:
        return config.mountpoint
    received = values["mountpoint_received"]
    if received in {"none", "legacy"}:
        return received
    if received != "-" and PurePosixPath(received).is_relative_to(config.mountpoint):
        return received
    return config.mountpoint + dataset.removeprefix(config.target_dataset)


def finalize(config: Config) -> None:
    """Make the completed target writable, mountable, and locally tagged."""

    # Anchor every mountpoint before releasing canmount. Reverting a mountpoint
    # from none to a real path can remount the dataset there and then, so no
    # source-absolute path may ever become the live value -- not even briefly.
    # zfs get -r lists parents before children, so each mountpoint below is set
    # after the one it nests under.
    planned = remote_properties(config)
    for dataset, values in planned.items():
        if values["type"] == "volume":
            continue
        remote_zfs(config, "set", f"mountpoint={desired_mountpoint(config, dataset, values)}", dataset)

    # Release the remaining pins. -S reverts each dataset to the value its
    # source carried rather than to this pool's inherited default; changing
    # either property never mounts anything on its own.
    for property_name in ("readonly", "canmount"):
        remote_zfs(config, "inherit", "-S", "-r", property_name, config.target_dataset)
    remote_zfs(config, "set", "readonly=off", "canmount=on", config.target_dataset)

    tagged = []
    for dataset, values in remote_properties(config).items():
        # A property-based mount check avoids invalid explicit mounts for
        # datasets whose received configuration uses none, legacy, or off.
        if values["mountpoint"] not in UNMOUNTABLE and values["canmount"] != "off" and values["mounted"] == "no":
            remote_zfs(config, "mount", dataset)

        # Received-source tags are invisible to the local snapshot picker.
        if values["autobackup:bak"] == "true":
            tagged.append(dataset)

    if tagged:
        remote_zfs(config, "set", "autobackup:bak=true", *tagged)


def main(argv: list[str]) -> None:
    """Drive confirmation, validation, transfer, and finalization."""

    config = parse_config(argv)
    sources = resolve_sources(config)
    check_target_preflight(config)

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
    for source in sources:
        sync_dataset(config, source)
    finalize(config)
    print("Restore complete. Converge the host next -- it re-asserts the remaining dataset properties.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RestoreError as error:
        sys.exit(f"ERROR: {error}")
    except KeyboardInterrupt:
        print(
            "ERROR: interrupted. A partial target tree may remain; abort any pending receive "
            "and destroy it before re-running (the next run prints the exact commands).",
            file=sys.stderr,
        )
        sys.exit(130)

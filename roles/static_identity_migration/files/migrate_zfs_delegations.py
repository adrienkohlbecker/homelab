#!/usr/bin/env python3
"""Remove ZFS delegated permissions retained under legacy numeric UIDs."""

import argparse
import pwd
import re
import subprocess
from dataclasses import dataclass

PERMISSIONS_HEADING = re.compile(r"^-+ Permissions on (?P<dataset>.+?) -+$")
UNKNOWN_USER = re.compile(r"^user \(unknown: (?P<uid>\d+)\)(?:\s|$)")
NAMED_USER = re.compile(r"^user (?P<name>[^\s(]+)(?:\s|$)")
SCOPE_OPTIONS = {
    "Local permissions:": "-l",
    "Descendent permissions:": "-d",
    "Local+Descendent permissions:": "-ld",
}


@dataclass(frozen=True, order=True)
class LegacyDelegation:
    dataset: str
    uid: int
    scope_option: str


def delegated_uid(line: str) -> int | None:
    if match := UNKNOWN_USER.match(line):
        return int(match.group("uid"))
    if match := NAMED_USER.match(line):
        try:
            return pwd.getpwnam(match.group("name")).pw_uid
        except KeyError:
            return None
    return None


def run_zfs(*args: str) -> str:
    return subprocess.run(
        ["zfs", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_legacy_delegations(
    output: str,
    dataset: str,
    legacy_uids: set[int],
) -> list[LegacyDelegation]:
    current_dataset = None
    scope_option = None
    delegations = set()

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if match := PERMISSIONS_HEADING.fullmatch(line):
            current_dataset = match.group("dataset")
            scope_option = None
            continue
        if current_dataset != dataset:
            continue
        if line in SCOPE_OPTIONS:
            scope_option = SCOPE_OPTIONS[line]
            continue
        uid = delegated_uid(line) if scope_option else None
        if uid in legacy_uids and scope_option is not None:
            delegations.add(LegacyDelegation(dataset, uid, scope_option))

    return sorted(delegations)


def find_legacy_delegations(legacy_uids: set[int]) -> list[LegacyDelegation]:
    datasets = run_zfs("list", "-H", "-o", "name", "-t", "filesystem,volume").splitlines()
    return [
        delegation
        for dataset in datasets
        for delegation in parse_legacy_delegations(
            run_zfs("allow", dataset),
            dataset,
            legacy_uids,
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uids", type=int, nargs="+", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    legacy_uids = set(args.uids)
    delegations = find_legacy_delegations(legacy_uids)
    for delegation in delegations:
        print(f"{delegation.dataset}: uid {delegation.uid} " f"scope {delegation.scope_option.removeprefix('-')}")
        if not args.dry_run:
            run_zfs(
                "unallow",
                delegation.scope_option,
                "-u",
                str(delegation.uid),
                delegation.dataset,
            )

    if not args.dry_run:
        remaining = find_legacy_delegations(legacy_uids)
        if remaining:
            raise RuntimeError(f"legacy ZFS delegations remain: {remaining}")

    print(f"legacy delegations found: {len(delegations)}")


if __name__ == "__main__":
    main()

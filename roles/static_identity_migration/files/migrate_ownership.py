#!/usr/bin/env python3
"""Remap legacy persistent-file ownership without crossing dataset mounts."""

import argparse
import grp
import json
import os
import pwd
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ActiveProcess:
    uid: int
    pid: int
    name: str


def paths_on_device(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        # Optional roots may disappear after the caller checks them.
        return
    root_device = root_stat.st_dev
    yield root, root_stat

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in list(dirnames):
            path = directory_path / name
            try:
                stat = path.lstat()
            except FileNotFoundError:
                # Concurrent writers may remove an entry listed by os.walk.
                continue
            if stat.st_dev != root_device:
                dirnames.remove(name)
                continue
            yield path, stat
        for name in filenames:
            path = directory_path / name
            try:
                stat = path.lstat()
            except FileNotFoundError:
                # Concurrent writers may remove an entry listed by os.walk.
                continue
            if stat.st_dev == root_device:
                yield path, stat


def migrate_path(
    root: Path,
    uid_map: dict[int, int],
    gid_map: dict[int, int],
    *,
    dry_run: bool,
) -> int:
    if not root.exists():
        return 0

    changed = 0
    for path, stat in paths_on_device(root):
        uid = uid_map.get(stat.st_uid, -1)
        gid = gid_map.get(stat.st_gid, -1)
        if uid == -1 and gid == -1:
            continue
        if not dry_run:
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                # A path removed after lstat needs no ownership migration.
                continue
        changed += 1
    return changed


def numeric_map(values: dict[str, Any]) -> dict[int, int]:
    return {int(old): int(new) for old, new in values.items()}


def identity_maps(entry: dict[str, Any]) -> tuple[dict[int, int], dict[int, int]]:
    name = entry["name"]
    target_uid = int(entry["uid"])
    target_gid = int(entry["gid"])
    old_uids = {int(value) for value in entry.get("old_uids", [])}
    old_gids = {int(value) for value in entry.get("old_gids", [])}

    # Retired identities legitimately have no current passwd entry.
    with suppress(KeyError):
        old_uids.add(pwd.getpwnam(name).pw_uid)
    # Retired identities legitimately have no current group entry.
    with suppress(KeyError):
        old_gids.add(grp.getgrnam(name).gr_gid)

    return (
        {value: target_uid for value in old_uids if value != target_uid},
        {value: target_gid for value in old_gids if value != target_gid},
    )


def configured_paths(config: dict[str, Any]) -> set[Path]:
    paths = {Path(raw_path) for entry in config["identities"] for raw_path in entry["paths"]}
    paths.update(Path(entry["path"]) for entry in config["paths"])
    return paths


def validate_required_mounts(config: dict[str, Any]) -> None:
    missing = [Path(raw_path) for raw_path in config.get("required_mounts", []) if not Path(raw_path).is_mount()]
    if missing:
        rendered = ", ".join(str(path) for path in missing)
        raise RuntimeError(f"required filesystems are not mounted: {rendered}")


def active_identity_uid_labels(config: dict[str, Any]) -> dict[int, set[str]]:
    labels: dict[int, set[str]] = {}
    for entry in config["identities"]:
        try:
            current_uid = pwd.getpwnam(entry["name"]).pw_uid
        except KeyError:
            # Retired identities cannot own a live process by account name.
            continue
        if current_uid != int(entry["uid"]):
            labels.setdefault(current_uid, set()).add(entry["name"])
    return labels


def active_processes(
    source_uids: set[int],
    *,
    proc_root: Path = Path("/proc"),
) -> list[ActiveProcess]:
    active = []
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            status = (process_dir / "status").read_text()
        except FileNotFoundError:
            # Processes can exit between listing /proc and reading status.
            continue

        fields = {
            key.rstrip(":"): value.strip() for key, value in (line.split(maxsplit=1) for line in status.splitlines())
        }
        process_uids = {int(value) for value in fields["Uid"].split()}
        active.extend(
            ActiveProcess(
                uid=uid,
                pid=int(process_dir.name),
                name=fields.get("Name", "unknown"),
            )
            for uid in process_uids & source_uids
        )
    return sorted(active, key=lambda process: (process.uid, process.pid))


def format_active_processes(active: list[ActiveProcess], labels: dict[int, set[str]]) -> str:
    lines = []
    for process in active:
        names = ", ".join(sorted(labels[process.uid]))
        lines.append(f"uid {process.uid} ({names}): {process.pid}/{process.name}")
    return "\n".join(lines)


def migrate(config: dict[str, Any], *, dry_run: bool) -> int:
    changed = 0
    for entry in config["identities"]:
        uid_map, gid_map = identity_maps(entry)
        for raw_path in entry["paths"]:
            changed += migrate_path(Path(raw_path), uid_map, gid_map, dry_run=dry_run)

    for entry in config["paths"]:
        changed += migrate_path(
            Path(entry["path"]),
            numeric_map(entry.get("uid_map", {})),
            numeric_map(entry.get("gid_map", {})),
            dry_run=dry_run,
        )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with args.config.open() as stream:
        config = json.load(stream)
    validate_required_mounts(config)

    missing_paths = sorted(str(path) for path in configured_paths(config) if not path.exists())
    if missing_paths:
        print(f"skipped missing optional paths: {', '.join(missing_paths)}")

    labels = active_identity_uid_labels(config)
    active = active_processes(set(labels))
    if active:
        rendered = format_active_processes(active, labels)
        if not args.dry_run:
            raise RuntimeError("source UIDs still own live processes; stop their services and " f"rerun:\n{rendered}")
        print(f"source UIDs with live processes:\n{rendered}")

    changed = migrate(config, dry_run=args.dry_run)
    print(f"ownership entries changed: {changed}")

    if args.marker and not args.dry_run:
        active = active_processes(set(labels))
        if active:
            rendered = format_active_processes(active, labels)
            raise RuntimeError(
                "source UIDs acquired processes during migration; ownership is "
                f"safe to migrate again and no marker was written:\n{rendered}"
            )
        args.marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.marker.with_suffix(".tmp")
        temporary.write_text(f"schema=1\nchanged={changed}\n")
        os.replace(temporary, args.marker)


if __name__ == "__main__":
    main()

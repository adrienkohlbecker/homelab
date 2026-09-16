"""Shared protocol primitives for published QEMU image bundles."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Any

BUNDLE_NAME = "disks.tar.zst"
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "promoted.json"
VALID_MACHINES = {"box", "box_deps", "lab"}


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv: list[str], **kwargs: Any) -> str:
    return run(argv, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def find_tar() -> str:
    for candidate in ("tar", "gtar"):
        path = shutil.which(candidate)
        if not path:
            continue
        probe = subprocess.run(
            [path, "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if "--zstd" in probe.stdout and "--sparse" in probe.stdout:
            return path
    sys.exit("required tar support missing: need GNU tar/bsdtar with --zstd and --sparse")


def validate_member_name(member: str) -> None:
    path = PurePosixPath(member)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        sys.exit(f"unsafe archive member path in manifest: {member!r}")


def manifest_files(manifest: dict[str, Any]) -> list[dict[str, str]]:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a non-empty list")

    normalized: list[dict[str, str]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entries must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("manifest file name must be a non-empty string")
        validate_member_name(name)
        normalized.append({"name": name})

    names = [entry["name"] for entry in normalized]
    if len(names) != len(set(names)):
        raise ValueError("manifest file names must be unique")
    return normalized

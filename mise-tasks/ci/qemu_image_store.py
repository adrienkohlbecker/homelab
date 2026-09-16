"""Shared protocol primitives for published QEMU image bundles."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import yaml

BUNDLE_NAME = "disks.tar.zst"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 2
POINTER_NAME = "promoted.json"
VALID_MACHINES = {"box", "box_deps", "lab"}
ARCHITECTURES: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "data" / "architectures.yml").read_text()
)
VALID_ARCHITECTURES = set(ARCHITECTURES)


class ImageStore(NamedTuple):
    """The regional S3 bucket that holds one architecture's image bundles."""

    bucket: str
    region: str


class ManifestBundle(NamedTuple):
    """The compressed bundle named and hashed by a manifest."""

    name: str
    sha256: str


class ManifestFile(NamedTuple):
    """One extracted bundle member and its expected size."""

    name: str
    size: int


def image_store(architecture: str) -> ImageStore:
    ci = ARCHITECTURES[architecture]["ci"]
    return ImageStore(bucket=ci["image_bucket"], region=ci["aws_region"])


def host_architecture() -> str:
    """Return this host's architecture in the uname-style names the stores use."""
    machine = platform.machine()
    return "aarch64" if machine == "arm64" else machine


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


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} sha256 is invalid")
    return value


def manifest_version(manifest: dict[str, Any]) -> int:
    version = manifest.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest format_version: {version!r}")
    return version


def manifest_bundle(manifest: dict[str, Any]) -> ManifestBundle:
    manifest_version(manifest)
    bundle = manifest.get("bundle")
    if not isinstance(bundle, dict):
        raise ValueError("manifest bundle must be an object")
    name = bundle.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("manifest bundle name must be a non-empty string")
    return ManifestBundle(name=name, sha256=_sha256(bundle.get("sha256"), "manifest bundle"))


def manifest_files(manifest: dict[str, Any]) -> list[ManifestFile]:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a non-empty list")

    manifest_version(manifest)
    normalized: list[ManifestFile] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entries must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("manifest file name must be a non-empty string")
        validate_member_name(name)
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"manifest file size is invalid for {name!r}")
        normalized.append(ManifestFile(name=name, size=size))

    names = [entry.name for entry in normalized]
    if len(names) != len(set(names)):
        raise ValueError("manifest file names must be unique")
    return normalized

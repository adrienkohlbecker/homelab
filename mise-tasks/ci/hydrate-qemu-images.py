#!/usr/bin/env python3
# fmt: off
#MISE description="Download the promoted qemu image bundle from S3 into the local harness cache"
#USAGE arg "<machine>" help="Promoted qemu image bundle: box, box_deps, or lab"
#USAGE complete "machine" run="printf 'box\nbox_deps\nlab\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
#USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
#USAGE flag "--architecture <architecture>" help="Guest architecture (x86_64 or aarch64)" default="x86_64"
#USAGE flag "--build-id <build_id>" help="Hydrate an immutable build directly instead of reading promoted.json"
#USAGE flag "--force" help="Re-download even when the local marker already matches"
# fmt: on
"""Hydrate the local qemu harness image cache from S3.

The aws_qemu cells populate their qemu harness images from the S3 bundles
selected by a pointer object, or by an explicit immutable build id when deriving
one image from another:

    s3://<bucket>/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}
    s3://<bucket>/<ubuntu>/<machine>/<build-id>/{manifest.json,disks.tar.zst}

The lab target does not call this: lab bakes write the artifacts into lab's
local /mnt/scratch/homelab_ci and its cells boot them in place.

An exclusive flock per machine/release keeps concurrent cells from downloading
or replacing the same image directory at the same time.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from qemu_image_store import (
    BUNDLE_NAME,
    MANIFEST_NAME,
    POINTER_NAME,
    VALID_ARCHITECTURES,
    VALID_MACHINES,
    find_tar,
    manifest_files,
    output,
    run,
    sha256,
    validate_member_name,
)

MARKER_NAME = ".homelab_s3_build_id"
LOCAL_MANIFEST_NAME = ".homelab_s3_manifest.json"
S3_BUCKET = "homelab-ci-images"
AWS_REGION = "eu-central-1"


@dataclasses.dataclass(frozen=True)
class ImageSelection:
    build_id: str
    source_sha: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("machine", choices=sorted(VALID_MACHINES))
    # choices, not just USAGE completion: an unsupported codename should fail
    # at parse rather than deep inside an S3 fetch for a prefix that is gone.
    parser.add_argument(
        "--ubuntu",
        choices=sorted(UBUNTU_RELEASES),
        default=os.environ.get("usage_ubuntu", DEFAULT_UBUNTU),
    )
    parser.add_argument("--bucket", default=os.environ.get("usage_bucket", S3_BUCKET))
    parser.add_argument("--region", default=os.environ.get("usage_region", AWS_REGION))
    parser.add_argument(
        "--architecture",
        choices=sorted(VALID_ARCHITECTURES),
        default=os.environ.get("usage_architecture", "x86_64"),
    )
    parser.add_argument("--build-id", default=os.environ.get("usage_build_id"))
    parser.add_argument(
        "--force",
        action="store_true",
        default=os.environ.get("usage_force") == "true",
    )
    return parser.parse_args()


def aws_base(args: argparse.Namespace) -> list[str]:
    return [
        "aws",
        "--region",
        args.region,
        "--cli-connect-timeout",
        "10",
        "--cli-read-timeout",
        "300",
    ]


def dest_root() -> Path:
    root = os.environ.get("HOMELAB_CI_DIR") or "/mnt/scratch/homelab_ci"
    return Path(root).expanduser().resolve()


def allows_legacy_provenance(args: argparse.Namespace) -> bool:
    return args.architecture == "x86_64" and args.bucket == S3_BUCKET and args.region == AWS_REGION


def validated_source_sha(document: dict[str, Any], args: argparse.Namespace, label: str) -> str | None:
    architecture = document.get("architecture")
    source_sha = document.get("source_sha")
    if architecture is None and allows_legacy_provenance(args):
        if source_sha is not None:
            sys.exit(f"{label} source_sha is present without architecture")
        return None
    if architecture != args.architecture:
        sys.exit(f"{label} architecture mismatch: expected {args.architecture!r}, got {architecture!r}")
    if (
        not isinstance(source_sha, str)
        or len(source_sha) not in (40, 64)
        or any(character not in "0123456789abcdef" for character in source_sha)
    ):
        sys.exit(f"{label} source_sha must be a full lowercase Git object id")
    return source_sha


def resolve_image(args: argparse.Namespace) -> ImageSelection:
    if args.build_id:
        return ImageSelection(build_id=args.build_id, source_sha=None)
    key = f"{args.ubuntu}/{args.machine}/{POINTER_NAME}"
    uri = f"s3://{args.bucket}/{key}"
    body = output([*aws_base(args), "s3", "cp", uri, "-"])
    if not body:
        sys.exit(f"missing or empty promoted pointer: {uri}")
    try:
        pointer = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid pointer JSON at {uri}: {exc}") from exc
    for field_name, expected in (("machine", args.machine), ("ubuntu", args.ubuntu)):
        if pointer.get(field_name) != expected:
            sys.exit(f"pointer {field_name} mismatch at {uri}: expected {expected!r}, got {pointer.get(field_name)!r}")
    build_id = pointer.get("build_id")
    if not isinstance(build_id, str) or not build_id:
        sys.exit(f"pointer build_id must be a non-empty string at {uri}")
    return ImageSelection(
        build_id=build_id,
        source_sha=validated_source_sha(pointer, args, f"pointer at {uri}"),
    )


def download_s3(args: argparse.Namespace, key: str, dest: Path) -> None:
    uri = f"s3://{args.bucket}/{key}"
    print(f"==> downloading {uri}")
    run(
        [
            *aws_base(args),
            "s3",
            "cp",
            uri,
            str(dest),
            "--only-show-errors",
            "--checksum-mode",
            "ENABLED",
        ]
    )


def read_manifest(path: Path, args: argparse.Namespace, selection: ImageSelection) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid manifest JSON in {path}: {exc}") from exc
    for key, expected in (("machine", args.machine), ("ubuntu", args.ubuntu), ("build_id", selection.build_id)):
        if manifest.get(key) != expected:
            sys.exit(f"manifest {key} mismatch: expected {expected!r}, got {manifest.get(key)!r}")
    manifest_source_sha = validated_source_sha(manifest, args, "manifest")
    if selection.source_sha is not None and manifest_source_sha != selection.source_sha:
        sys.exit(f"manifest source_sha mismatch: expected {selection.source_sha!r}, got {manifest_source_sha!r}")
    try:
        manifest_files(manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return manifest


def validate_archive_members(tar: str, bundle: Path, expected_members: list[str]) -> None:
    listed = output([tar, "--zstd", "-tf", str(bundle)]).splitlines()
    for member in listed:
        validate_member_name(member)
    # Names alone do not bound where tar writes: a symlink member plus a member
    # nested beneath it both pass the name check while the second lands outside
    # the staging dir. A qemu image bundle only ever holds regular files, so
    # require that -- the verbose listing's first column is the type flag.
    for line in output([tar, "--zstd", "-tvf", str(bundle)]).splitlines():
        if not line.startswith("-"):
            sys.exit(f"archive member is not a regular file: {line!r}")
    expected = set(expected_members)
    actual = set(listed)
    if actual != expected:
        missing = " ".join(sorted(expected - actual)) or "(none)"
        extra = " ".join(sorted(actual - expected)) or "(none)"
        sys.exit(f"archive members do not match manifest; missing: {missing}; extra: {extra}")


def verify_files(root: Path, files: list[dict[str, str]]) -> None:
    for entry in files:
        path = root / entry["name"]
        if not path.is_file():
            sys.exit(f"bundle did not extract expected member: {entry['name']}")
        actual = sha256(path)
        if actual != entry["sha256"]:
            sys.exit(f"bundle sha256 mismatch for {entry['name']}: expected {entry['sha256']}, got {actual}")


def local_cache_complete(target: Path, args: argparse.Namespace, selection: ImageSelection) -> bool:
    marker = target / MARKER_NAME
    manifest_path = target / LOCAL_MANIFEST_NAME
    if not marker.is_file() or not manifest_path.is_file():
        return False
    if marker.read_text().strip() != selection.build_id:
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
        for key, expected in (
            ("machine", args.machine),
            ("ubuntu", args.ubuntu),
            ("build_id", selection.build_id),
        ):
            if manifest.get(key) != expected:
                return False
        manifest_source_sha = validated_source_sha(manifest, args, "cached manifest")
        if selection.source_sha is not None and manifest_source_sha != selection.source_sha:
            return False
        files = manifest_files(manifest)
    except json.JSONDecodeError, ValueError, SystemExit:
        return False
    return all((target / entry["name"]).is_file() for entry in files)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def replace_target(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    old: Path | None = None
    if target.exists() or target.is_symlink():
        old = target.with_name(f".{target.name}.old-{os.getpid()}")
        if old.exists() or old.is_symlink():
            remove_path(old)
        target.rename(old)
    staged.rename(target)
    if old is not None:
        # Best-effort cleanup of the old cache path after the replacement is live.
        with contextlib.suppress(OSError):
            remove_path(old)


def main() -> int:
    args = parse_args()
    if not shutil.which("aws"):
        sys.exit("required tool not found on PATH: aws")
    tar = find_tar()

    root = dest_root()
    lock_dir = root / ".hydrate-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{args.ubuntu}.{args.machine}.lock"
    target = root / args.ubuntu / args.machine

    with lock_path.open("w") as lock:
        print(f"==> waiting for hydrate lock {lock_path}")
        fcntl.flock(lock, fcntl.LOCK_EX)

        selection = resolve_image(args)
        if not args.force and local_cache_complete(target, args, selection):
            print(f"==> {target} already hydrated for {selection.build_id}")
            return 0

        prefix = f"{args.ubuntu}/{args.machine}/{selection.build_id}"
        print(f"==> hydrating {target} from s3://{args.bucket}/{prefix}/")
        with tempfile.TemporaryDirectory(prefix=f".hydrate-{args.ubuntu}-{args.machine}-", dir=root) as tmp:
            tmpdir = Path(tmp)
            manifest_path = tmpdir / MANIFEST_NAME
            bundle_path = tmpdir / BUNDLE_NAME
            staged = tmpdir / "image"
            staged.mkdir()

            download_s3(args, f"{prefix}/{MANIFEST_NAME}", manifest_path)
            manifest = read_manifest(manifest_path, args, selection)
            files = manifest_files(manifest)
            members = [entry["name"] for entry in files]
            bundle_name = manifest.get("bundle_name", BUNDLE_NAME)
            if not isinstance(bundle_name, str) or not bundle_name:
                sys.exit("manifest bundle_name must be a non-empty string")
            download_s3(args, f"{prefix}/{bundle_name}", bundle_path)

            print(f"==> extracting {bundle_name}")
            validate_archive_members(tar, bundle_path, members)
            run([tar, "--sparse", "--zstd", "--no-same-owner", "-xf", str(bundle_path), "-C", str(staged)])
            verify_files(staged, files)

            (staged / LOCAL_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            (staged / MARKER_NAME).write_text(f"{selection.build_id}\n")
            replace_target(staged, target)
            print(f"==> hydrated {target} for {selection.build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

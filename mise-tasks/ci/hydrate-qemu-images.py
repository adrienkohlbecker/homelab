#!/usr/bin/env python3
# MISE description="Download the promoted qemu image bundle from S3 into the local harness cache"
# USAGE arg "<machine>" help="Packer source/artifact name: box, box_deps, lab, or pug"
# USAGE complete "machine" run="printf 'box\nbox_deps\nlab\npug\n'"
# USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
# USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
# USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
# USAGE flag "--build-id <build_id>" help="Exact build id to hydrate; default resolves the promoted.json pointer"
# USAGE flag "--dest-root <path>" help="Harness image root; default is $HOMELAB_CI_DIR or /mnt/scratch/homelab_ci"
# USAGE flag "--force" help="Re-download even when the local marker already matches"
"""Hydrate the local qemu harness image cache from S3.

The aws_qemu cells populate their qemu harness images from the S3 bundles
selected by a pointer object:

    s3://homelab-ci-images/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}
    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/{manifest.json,disks.tar.zst}

The lab target does not call this: lab bakes write the artifacts into lab's
local /mnt/scratch/homelab_ci and its cells boot them in place.

An exclusive flock per machine/release keeps concurrent cells from downloading
or replacing the same image directory at the same time.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

BUNDLE_NAME = "disks.tar.zst"
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "promoted.json"
MARKER_NAME = ".homelab_s3_build_id"
LOCAL_MANIFEST_NAME = ".homelab_s3_manifest.json"
VALID_MACHINES = {"box", "box_deps"}


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv: list[str], **kwargs: Any) -> str:
    return run(argv, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("machine", choices=sorted(VALID_MACHINES))
    parser.add_argument("--ubuntu", default=os.environ.get("usage_ubuntu", "jammy"))
    parser.add_argument("--bucket", default=os.environ.get("usage_bucket", "homelab-ci-images"))
    parser.add_argument("--region", default=os.environ.get("usage_region", "eu-central-1"))
    parser.add_argument("--build-id", default=os.environ.get("usage_build_id"))
    parser.add_argument("--dest-root", default=os.environ.get("usage_dest_root"))
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


def dest_root(args: argparse.Namespace) -> Path:
    root = args.dest_root or os.environ.get("HOMELAB_CI_DIR") or "/mnt/scratch/homelab_ci"
    return Path(root).expanduser().resolve()


def resolve_build_id(args: argparse.Namespace) -> str:
    if args.build_id:
        return args.build_id
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
    return build_id


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


def read_manifest(path: Path, args: argparse.Namespace, build_id: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid manifest JSON in {path}: {exc}") from exc
    for key, expected in (("machine", args.machine), ("ubuntu", args.ubuntu), ("build_id", build_id)):
        if manifest.get(key) != expected:
            sys.exit(f"manifest {key} mismatch: expected {expected!r}, got {manifest.get(key)!r}")
    members = manifest.get("tar_members")
    if not isinstance(members, list) or not all(isinstance(member, str) and member for member in members):
        sys.exit("manifest tar_members must be a non-empty string list")
    for member in members:
        validate_member_name(member)
    return manifest


def validate_member_name(member: str) -> None:
    path = PurePosixPath(member)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        sys.exit(f"unsafe archive member path in manifest: {member!r}")


def validate_archive_members(tar: str, bundle: Path, expected_members: list[str]) -> None:
    listed = output([tar, "--zstd", "-tf", str(bundle)]).splitlines()
    for member in listed:
        validate_member_name(member)
    expected = set(expected_members)
    actual = set(listed)
    if actual != expected:
        missing = " ".join(sorted(expected - actual)) or "(none)"
        extra = " ".join(sorted(actual - expected)) or "(none)"
        sys.exit(f"archive members do not match manifest; missing: {missing}; extra: {extra}")


def local_cache_complete(target: Path, build_id: str) -> bool:
    marker = target / MARKER_NAME
    manifest_path = target / LOCAL_MANIFEST_NAME
    if not marker.is_file() or not manifest_path.is_file():
        return False
    if marker.read_text().strip() != build_id:
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return False
    return all((target / member).is_file() for member in manifest.get("tar_members", []))


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
        try:
            remove_path(old)
        except OSError:
            pass


def main() -> int:
    args = parse_args()
    if not shutil.which("aws"):
        sys.exit("required tool not found on PATH: aws")
    tar = find_tar()

    root = dest_root(args)
    lock_dir = root / ".hydrate-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{args.ubuntu}.{args.machine}.lock"
    target = root / args.ubuntu / args.machine

    with lock_path.open("w") as lock:
        print(f"==> waiting for hydrate lock {lock_path}")
        fcntl.flock(lock, fcntl.LOCK_EX)

        build_id = resolve_build_id(args)
        if not args.force and local_cache_complete(target, build_id):
            print(f"==> {target} already hydrated for {build_id}")
            return 0

        prefix = f"{args.ubuntu}/{args.machine}/{build_id}"
        print(f"==> hydrating {target} from s3://{args.bucket}/{prefix}/")
        with tempfile.TemporaryDirectory(prefix=f".hydrate-{args.ubuntu}-{args.machine}-", dir=root) as tmp:
            tmpdir = Path(tmp)
            manifest_path = tmpdir / MANIFEST_NAME
            bundle_path = tmpdir / BUNDLE_NAME
            staged = tmpdir / "image"
            staged.mkdir()

            download_s3(args, f"{prefix}/{MANIFEST_NAME}", manifest_path)
            manifest = read_manifest(manifest_path, args, build_id)
            bundle_name = manifest.get("bundle_name", BUNDLE_NAME)
            if not isinstance(bundle_name, str) or not bundle_name:
                sys.exit("manifest bundle_name must be a non-empty string")
            download_s3(args, f"{prefix}/{bundle_name}", bundle_path)

            print(f"==> extracting {bundle_name}")
            validate_archive_members(tar, bundle_path, manifest["tar_members"])
            run([tar, "--sparse", "--zstd", "-xf", str(bundle_path), "-C", str(staged)])
            for member in manifest["tar_members"]:
                if not (staged / member).is_file():
                    sys.exit(f"bundle did not extract expected member: {member}")

            (staged / LOCAL_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            (staged / MARKER_NAME).write_text(f"{build_id}\n")
            replace_target(staged, target)
            print(f"==> hydrated {target} for {build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

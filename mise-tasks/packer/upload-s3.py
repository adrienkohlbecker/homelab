#!/usr/bin/env python3
# MISE description="Bundle a published packer qemu artifact and upload it to the S3 image bucket"
# USAGE arg "<machine>" help="Packer source/artifact name: box or box_deps"
# USAGE complete "machine" run="printf 'box\nbox_deps\n'"
# USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
# USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
# USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
# USAGE flag "--build-id <build_id>" help="Immutable S3 build id; default is timestamp + current git SHA"
# USAGE flag "--artifact-dir <path>" help="Artifact dir to bundle; default is $HOMELAB_CI_DIR/<ubuntu>/<machine>"
# USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build id"
# USAGE flag "--dry-run" help="Build and print the manifest plan without creating the tarball, uploading, or promoting"
"""Upload qemu packer artifacts to the nested-CI S3 bundle layout.

The nested-qemu runner design uses S3 as the source of truth for qemu fixture
images for the aws_qemu target:

    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/manifest.json
    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/disks.tar.zst

The tarball contains the packer-ubuntu-N.{raw,qcow2} disks plus efivars.fd,
because the qemu harness copies efivars.fd from the same artifact directory
before booting ZFS-root variants. The manifest records the actual disk format
so Linux-built raw images and macOS-built qcow2 images remain explicit.

The live build for each machine/release pair is selected by a pointer object
(not SSM) stored inside the bucket itself:

    s3://<bucket>/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}

The lab target does not read S3: lab bakes write the artifacts into lab's local
/mnt/scratch/homelab_ci and its cells boot them in place, so only the aws_qemu
cells hydrate from these objects. The lab bake still uploads here so S3 stays
the canonical promoted store.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_NAME = "disks.tar.zst"
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "promoted.json"
S3_CHECKSUM_ALGORITHM = "SHA256"
VALID_MACHINES = {"box", "box_deps"}


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv: list[str], **kwargs: Any) -> str:
    return run(argv, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def git_output(args: list[str], default: str = "unknown") -> str:
    try:
        return output(["git", "-C", str(REPO_ROOT), *args])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return default


def default_build_id() -> str:
    sha = git_output(["rev-parse", "--short=12", "HEAD"])
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    pipeline = os.environ.get("CI_PIPELINE_ID")
    prefix = f"ci-{pipeline}" if pipeline else stamp
    return f"{prefix}-g{sha}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("machine", choices=sorted(VALID_MACHINES))
    parser.add_argument("--ubuntu", default=os.environ.get("usage_ubuntu", "jammy"))
    parser.add_argument("--bucket", default=os.environ.get("usage_bucket", "homelab-ci-images"))
    parser.add_argument("--region", default=os.environ.get("usage_region", "eu-central-1"))
    parser.add_argument("--build-id", default=os.environ.get("usage_build_id") or default_build_id())
    parser.add_argument("--artifact-dir", default=os.environ.get("usage_artifact_dir"))
    parser.add_argument(
        "--promote",
        action="store_true",
        default=os.environ.get("usage_promote") == "true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("usage_dry_run") == "true",
    )
    return parser.parse_args()


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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def qemu_info(path: Path) -> dict[str, Any]:
    try:
        raw = output(["qemu-img", "info", "--output=json", str(path)])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def disk_entry(path: Path) -> dict[str, Any]:
    info = qemu_info(path)
    return {
        "name": path.name,
        "format": info.get("format") or path.suffix.removeprefix("."),
        "size_bytes": path.stat().st_size,
        "virtual_size_bytes": info.get("virtual-size") or path.stat().st_size,
        "sha256": sha256(path),
    }


def artifact_dir(args: argparse.Namespace) -> Path:
    if args.artifact_dir:
        return Path(args.artifact_dir).expanduser().resolve()
    base = os.environ.get("HOMELAB_CI_DIR")
    if not base:
        base = str(REPO_ROOT / "packer" / "artifacts")
    return (Path(base) / args.ubuntu / args.machine).resolve()


def collect_artifact_files(root: Path) -> tuple[list[Path], Path]:
    if not root.is_dir():
        sys.exit(f"artifact directory does not exist: {root}")
    disks = sorted(root.glob("packer-ubuntu-*.raw")) + sorted(root.glob("packer-ubuntu-*.qcow2"))
    if not disks:
        sys.exit(f"no packer-ubuntu-*.{{raw,qcow2}} disks found in {root}")
    efivars = root / "efivars.fd"
    if not efivars.is_file():
        sys.exit(f"missing efivars.fd in {root}; qemu harness expects it beside the disks")
    return disks, efivars


def build_manifest(
    *,
    args: argparse.Namespace,
    root: Path,
    disks: list[Path],
    efivars: Path,
    s3_prefix: str,
) -> dict[str, Any]:
    full_sha = git_output(["rev-parse", "HEAD"])
    dirty = bool(git_output(["status", "--short"], default=""))
    return {
        "bundle_format_version": 1,
        "bundle_name": BUNDLE_NAME,
        "bundle_format": "tar+zstd",
        "s3_checksum_algorithm": S3_CHECKSUM_ALGORITHM,
        "machine": args.machine,
        "ubuntu": args.ubuntu,
        "build_id": args.build_id,
        "source_git_sha": full_sha,
        "source_git_dirty": dirty,
        "created_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "artifact_dir": str(root),
        "s3_bucket": args.bucket,
        "s3_prefix": s3_prefix,
        "pointer_key": f"{args.ubuntu}/{args.machine}/{POINTER_NAME}",
        "disks": [disk_entry(path) for path in disks],
        "support_files": [{"name": efivars.name, "size_bytes": efivars.stat().st_size, "sha256": sha256(efivars)}],
        "tar_members": [path.name for path in [*disks, efivars]],
    }


def create_bundle(tar: str, root: Path, members: list[str], bundle: Path) -> None:
    print(f"==> creating {bundle}")
    run([tar, "--sparse", "--zstd", "-cf", str(bundle), "-C", str(root), *members])


def aws_argv(region: str, *args: str) -> list[str]:
    return ["aws", "--region", region, *args]


def assert_new_object(bucket: str, key: str, region: str) -> None:
    result = subprocess.run(
        aws_argv(region, "s3api", "head-object", "--bucket", bucket, "--key", key),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0:
        sys.exit(f"refusing to overwrite existing object: s3://{bucket}/{key}")


def upload_file(bucket: str, path: Path, key: str, region: str, content_type: str | None = None) -> None:
    args = [
        "s3",
        "cp",
        str(path),
        f"s3://{bucket}/{key}",
        "--only-show-errors",
        "--checksum-algorithm",
        S3_CHECKSUM_ALGORITHM,
    ]
    if content_type:
        args += ["--content-type", content_type]
    print(f"==> uploading s3://{bucket}/{key}")
    run(aws_argv(region, *args))


def read_pointer(bucket: str, key: str, region: str) -> str | None:
    """Return the raw current pointer body, or None when absent/empty."""
    result = subprocess.run(
        aws_argv(region, "s3", "cp", f"s3://{bucket}/{key}", "-"),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout or None


def write_pointer(bucket: str, key: str, body: str, region: str) -> None:
    print(f"==> writing pointer s3://{bucket}/{key}")
    run(
        aws_argv(
            region,
            "s3",
            "cp",
            "-",
            f"s3://{bucket}/{key}",
            "--only-show-errors",
            "--content-type",
            "application/json",
        )
        + ["--checksum-algorithm", S3_CHECKSUM_ALGORITHM],
        input=body,
    )


def pointer_body(args: argparse.Namespace) -> str:
    return (
        json.dumps(
            {"build_id": args.build_id, "machine": args.machine, "ubuntu": args.ubuntu},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    args = parse_args()
    root = artifact_dir(args)
    disks, efivars = collect_artifact_files(root)
    s3_prefix = f"{args.ubuntu}/{args.machine}/{args.build_id}"
    bundle_key = f"{s3_prefix}/{BUNDLE_NAME}"
    manifest_key = f"{s3_prefix}/{MANIFEST_NAME}"
    pointer_key = f"{args.ubuntu}/{args.machine}/{POINTER_NAME}"

    print(f"artifact: {root}")
    print(f"target:   s3://{args.bucket}/{s3_prefix}/")
    manifest = build_manifest(args=args, root=root, disks=disks, efivars=efivars, s3_prefix=s3_prefix)

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        if args.promote:
            print(f"DRY RUN: would write pointer {pointer_key} -> {args.build_id}")
        return 0

    if not shutil.which("aws"):
        sys.exit("required tool not found on PATH: aws")
    tar = find_tar()

    # Immutability: never overwrite a published build.
    assert_new_object(args.bucket, bundle_key, args.region)
    assert_new_object(args.bucket, manifest_key, args.region)

    with tempfile.TemporaryDirectory(prefix="packer-s3-", dir=os.environ.get("TMPDIR")) as tmp:
        tmpdir = Path(tmp)
        bundle = tmpdir / BUNDLE_NAME
        manifest_path = tmpdir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        create_bundle(tar, root, manifest["tar_members"], bundle)

        upload_file(args.bucket, bundle, bundle_key, args.region, "application/zstd")
        upload_file(args.bucket, manifest_path, manifest_key, args.region, "application/json")

    if args.promote:
        prev = read_pointer(args.bucket, pointer_key, args.region)
        write_pointer(args.bucket, pointer_key, pointer_body(args), args.region)
        if prev is not None:
            print(f"==> previous pointer: {prev.strip()}")
    else:
        print("==> upload complete; promotion pending (re-run with --promote)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

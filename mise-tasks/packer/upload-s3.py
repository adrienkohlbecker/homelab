#!/usr/bin/env python3
# MISE description="Bundle a published packer qemu artifact and upload it to the S3 image bucket"
# USAGE arg "<machine>" help="Packer source/artifact name: box, box_deps, or lab"
# USAGE complete "machine" run="printf 'box\nbox_deps\nlab\n'"
# USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
# USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
# USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
# USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
# USAGE flag "--build-id <build_id>" help="Immutable S3 build id; default is pipeline.job in CI or timestamp + current git SHA"
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
before booting ZFS-root variants. The manifest records a SHA-256 for every
member so hydration can reject corrupted artifacts before making them live.

The live build for each machine/release pair is selected by a pointer object
(not SSM) stored inside the bucket itself:

    s3://<bucket>/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}

The lab target does not read S3: lab bakes write the artifacts into lab's local
/mnt/scratch/homelab_ci and its cells boot them in place, so only the aws_qemu
cells hydrate from these objects. The lab bake still uploads here so S3 stays
the canonical promoted store. Uploaded objects start as candidates. Promotion
tags the current build and three rollback builds as retained, marks older builds
expirable, and records the rollback ids in the pointer. S3 lifecycle performs
the eventual deletion after the seven-day recovery window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
S3_CHECKSUM_ALGORITHM = "SHA256"
IMAGE_STATE_TAG = "qemu_image_state"
CANDIDATE_STATE = "candidate"
RETAINED_STATE = "retained"
EXPIRABLE_STATE = "expirable"
RETAINED_BUILD_COUNT = 4
sys.path.insert(0, str(REPO_ROOT / "mise-tasks" / "ci"))
sys.path.insert(0, str(REPO_ROOT / "test"))
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES  # noqa: E402
from qemu_image_store import (  # noqa: E402
    BUNDLE_NAME,
    MANIFEST_NAME,
    POINTER_NAME,
    VALID_MACHINES,
    find_tar,
    output,
    run,
    sha256,
)


def git_output(args: list[str], default: str = "unknown") -> str:
    try:
        return output(["git", "-C", str(REPO_ROOT), *args])
    except subprocess.CalledProcessError, FileNotFoundError:
        return default


def default_build_id() -> str:
    pipeline = os.environ.get("CI_PIPELINE_ID")
    job = os.environ.get("CI_JOB_ID")
    if pipeline and job:
        return f"{pipeline}.{job}"
    sha = git_output(["rev-parse", "--short=12", "HEAD"])
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-g{sha}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("machine", choices=sorted(VALID_MACHINES))
    # choices, not just USAGE completion: a stale codename should fail at parse
    # rather than write a bundle under an S3 prefix nothing will ever read.
    parser.add_argument(
        "--ubuntu",
        choices=sorted(UBUNTU_RELEASES),
        default=os.environ.get("usage_ubuntu", DEFAULT_UBUNTU),
    )
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
    disks: list[Path],
    efivars: Path,
) -> dict[str, Any]:
    files = [*disks, efivars]
    return {
        "bundle_name": BUNDLE_NAME,
        "machine": args.machine,
        "ubuntu": args.ubuntu,
        "build_id": args.build_id,
        "files": [{"name": path.name, "sha256": sha256(path)} for path in files],
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


def tag_object(bucket: str, key: str, state: str, region: str) -> None:
    print(f"==> tagging s3://{bucket}/{key} {IMAGE_STATE_TAG}={state}")
    run(
        aws_argv(
            region,
            "s3api",
            "put-object-tagging",
            "--bucket",
            bucket,
            "--key",
            key,
            "--tagging",
            json.dumps({"TagSet": [{"Key": IMAGE_STATE_TAG, "Value": state}]}),
        )
    )


def list_build_objects(bucket: str, machine: str, ubuntu: str, region: str) -> dict[str, dict[str, Any]]:
    """Return objects grouped by immutable build id under one image prefix."""
    prefix = f"{ubuntu}/{machine}/"
    response = json.loads(
        output(
            aws_argv(
                region,
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--prefix",
                prefix,
                "--output",
                "json",
            )
        )
    )
    builds: dict[str, dict[str, Any]] = {}
    for item in response.get("Contents", []):
        relative = item["Key"].removeprefix(prefix)
        build_id, separator, _name = relative.partition("/")
        if not separator or not build_id:
            continue
        build = builds.setdefault(build_id, {"last_modified": "", "keys": []})
        build["last_modified"] = max(build["last_modified"], item["LastModified"])
        build["keys"].append(item["Key"])
    return builds


def select_retained_builds(builds: dict[str, dict[str, Any]], promoted_build_id: str) -> list[str]:
    """Select the promoted build and its newest available rollback builds."""
    if promoted_build_id not in builds:
        raise RuntimeError(f"uploaded build is missing from S3 listing: {promoted_build_id}")
    newest = sorted(builds, key=lambda build_id: builds[build_id]["last_modified"], reverse=True)
    return [promoted_build_id, *(build_id for build_id in newest if build_id != promoted_build_id)][
        :RETAINED_BUILD_COUNT
    ]


def tag_builds(
    bucket: str,
    builds: dict[str, dict[str, Any]],
    build_ids: list[str],
    region: str,
    *,
    state: str,
) -> None:
    for build_id in build_ids:
        for key in builds[build_id]["keys"]:
            tag_object(bucket, key, state, region)


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
        [
            *aws_argv(
                region,
                "s3",
                "cp",
                "-",
                f"s3://{bucket}/{key}",
                "--only-show-errors",
                "--content-type",
                "application/json",
            ),
            "--checksum-algorithm",
            S3_CHECKSUM_ALGORITHM,
        ],
        input=body,
    )


def pointer_body(args: argparse.Namespace, retained_build_ids: list[str]) -> str:
    return (
        json.dumps(
            {
                "build_id": args.build_id,
                "machine": args.machine,
                "rollback_build_ids": retained_build_ids[1:],
                "ubuntu": args.ubuntu,
            },
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
    manifest = build_manifest(args=args, disks=disks, efivars=efivars)

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
        create_bundle(tar, root, [entry["name"] for entry in manifest["files"]], bundle)

        upload_file(args.bucket, bundle, bundle_key, args.region, "application/zstd")
        tag_object(args.bucket, bundle_key, CANDIDATE_STATE, args.region)
        upload_file(args.bucket, manifest_path, manifest_key, args.region, "application/json")
        tag_object(args.bucket, manifest_key, CANDIDATE_STATE, args.region)

    if args.promote:
        prev = read_pointer(args.bucket, pointer_key, args.region)
        builds = list_build_objects(args.bucket, args.machine, args.ubuntu, args.region)
        retained = select_retained_builds(builds, args.build_id)
        tag_builds(
            args.bucket,
            builds,
            retained,
            args.region,
            state=RETAINED_STATE,
        )
        write_pointer(args.bucket, pointer_key, pointer_body(args, retained), args.region)
        expirable = [build_id for build_id in builds if build_id not in retained]
        tag_builds(
            args.bucket,
            builds,
            expirable,
            args.region,
            state=EXPIRABLE_STATE,
        )
        if prev is not None:
            print(f"==> previous pointer: {prev.strip()}")
    else:
        print("==> upload complete; promotion pending (re-run with --promote)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

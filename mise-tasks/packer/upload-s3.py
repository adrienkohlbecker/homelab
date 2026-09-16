#!/usr/bin/env python3
# fmt: off
#MISE description="Bundle a published packer qemu artifact and upload it to the S3 image bucket"
#USAGE arg "<machine>" help="Packer source/artifact name: box, box_deps, or lab"
#USAGE complete "machine" run="printf 'box\nbox_deps\nlab\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--architecture <architecture>" help="Guest architecture (x86_64 or aarch64); must match this build host, which is the default, and selects the image store"
#USAGE flag "--build-id <build_id>" help="Immutable S3 build id; default is pipeline.job in CI or timestamp + current git SHA"
#USAGE flag "--artifact-dir <path>" help="Artifact dir to bundle; default is $HOMELAB_CI_DIR/<ubuntu>/<machine>"
#USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build id"
#USAGE flag "--dry-run" help="Build and print the manifest plan without creating the tarball, uploading, or promoting"
#USAGE flag "--preflight" help="Validate the upload target and exit before reading artifacts or S3"
# fmt: on
"""Upload qemu packer artifacts to the nested-CI S3 bundle layout.

The nested-qemu runner design uses S3 as the source of truth for qemu fixture
images for the aws_qemu target. Both architectures use one regional bucket,
with separate key prefixes:

    s3://<bucket>/<arch-prefix>/<ubuntu>/<machine>/<build-id>/manifest.json
    s3://<bucket>/<arch-prefix>/<ubuntu>/<machine>/<build-id>/disks.tar.zst

The tarball contains the packer-ubuntu-N.{raw,qcow2} disks plus efivars.fd,
because the qemu harness copies efivars.fd from the same artifact directory
before booting ZFS-root variants. The manifest records one SHA-256 for the
immutable archive so hydration can verify the download while extracting it.

The live build for each machine/release pair is selected by a pointer object
(not SSM) stored inside the bucket itself:

    s3://<bucket>/<arch-prefix>/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}

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
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
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
    MANIFEST_VERSION,
    POINTER_NAME,
    VALID_ARCHITECTURES,
    VALID_MACHINES,
    find_tar,
    host_architecture,
    image_prefix,
    image_store,
    output,
    run,
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


def source_sha() -> str:
    """Return the commit recorded as the image's provenance.

    Outside CI, HEAD only describes the artifact when tracked files match it,
    so a modified checkout is refused instead of stamped with a commit that
    did not build the image.
    """
    sha = os.environ.get("CI_COMMIT_SHA")
    if not sha:
        if git_output(["status", "--porcelain", "--untracked-files=no"], default=""):
            sys.exit("refusing to record HEAD as source_sha: tracked files have uncommitted changes")
        sha = git_output(["rev-parse", "HEAD"], default="")
    if len(sha) not in (40, 64) or any(character not in "0123456789abcdef" for character in sha):
        sys.exit(f"source SHA must be a full lowercase Git object id, got {sha!r}")
    return sha


def validate_target(args: argparse.Namespace) -> None:
    """Refuse an architecture label that cannot match the artifact.

    Qemu fixtures are built natively, so the upload host's architecture is the
    artifact's. Hydration trusts the recorded label, and the label also selects
    the S3 key prefix.
    """
    host = host_architecture()
    if args.architecture != host:
        sys.exit(f"refusing to label an artifact built on this {host} host as {args.architecture}")


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
    parser.add_argument(
        "--architecture",
        choices=sorted(VALID_ARCHITECTURES),
        default=os.environ.get("usage_architecture") or host_architecture(),
    )
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
    parser.add_argument(
        "--preflight",
        action="store_true",
        default=os.environ.get("usage_preflight") == "true",
    )
    args = parser.parse_args()
    store = image_store(args.architecture)
    args.bucket, args.region = store.bucket, store.region
    return args


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
    bundle_sha256: str | None,
) -> dict[str, Any]:
    files = [*disks, efivars]
    return {
        "format_version": MANIFEST_VERSION,
        "bundle": {"name": BUNDLE_NAME, "sha256": bundle_sha256},
        "machine": args.machine,
        "ubuntu": args.ubuntu,
        "build_id": args.build_id,
        "architecture": args.architecture,
        "source_sha": args.source_sha,
        "files": [{"name": path.name, "size": path.stat().st_size} for path in files],
    }


def upload_bundle(tar: str, root: Path, files: list[Path], bucket: str, key: str, region: str) -> str:
    """Hash the compressed tar stream while uploading it, without staging an archive."""
    uri = f"s3://{bucket}/{key}"
    print(f"==> streaming bundle to {uri}")
    upload_args = [
        *aws_argv(region, "s3", "cp", "-", uri),
        "--only-show-errors",
        "--checksum-algorithm",
        S3_CHECKSUM_ALGORITHM,
        "--content-type",
        "application/zstd",
    ]
    source_size = sum(path.stat().st_size for path in files)
    # A conservative size hint avoids S3's 10,000-part limit near 50 GiB.
    if source_size > 45 * 1024**3:
        upload_args += ["--expected-size", str(source_size + 1024**3)]
    tar_args = [tar, "--sparse", "--zstd", "-cf", "-", "-C", str(root), *(path.name for path in files)]
    producer = subprocess.Popen(tar_args, stdout=subprocess.PIPE)
    consumer: subprocess.Popen[bytes] | None = None
    completed = False
    try:
        consumer = subprocess.Popen(upload_args, stdin=subprocess.PIPE)
        assert producer.stdout is not None
        assert consumer.stdin is not None
        digest = hashlib.sha256()
        while chunk := producer.stdout.read(1024 * 1024):
            consumer.stdin.write(chunk)
            digest.update(chunk)
        if producer.wait() != 0:
            raise subprocess.CalledProcessError(producer.returncode, tar_args)
        consumer.stdin.close()
        if consumer.wait() != 0:
            raise subprocess.CalledProcessError(consumer.returncode, upload_args)
        completed = True
        return digest.hexdigest()
    finally:
        if producer.stdout is not None:
            producer.stdout.close()
        if consumer is not None:
            if not completed and consumer.poll() is None:
                consumer.terminate()
            if consumer.stdin is not None and not consumer.stdin.closed:
                # A failed upload may close its pipe before buffered bytes flush.
                with contextlib.suppress(BrokenPipeError):
                    consumer.stdin.close()
            if consumer.poll() is None:
                consumer.wait()
        if producer.poll() is None:
            producer.terminate()
            producer.wait()


def aws_argv(region: str, *args: str) -> list[str]:
    return ["aws", "--region", region, *args]


def conditional_put(
    bucket: str,
    path: Path,
    key: str,
    region: str,
    *,
    content_type: str,
    precondition: list[str],
    conflict: str,
) -> None:
    """Upload *path* only if the S3 write precondition holds.

    S3 rejects a failed ``If-Match``/``If-None-Match`` with 412 (or 409 when
    another conditional write to the key is in flight); both exit with
    *conflict* instead of a traceback, and any other failure propagates.
    """
    result = subprocess.run(
        aws_argv(
            region,
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(path),
            "--content-type",
            content_type,
            "--checksum-algorithm",
            S3_CHECKSUM_ALGORITHM,
            *precondition,
        ),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode == 0:
        return
    if "PreconditionFailed" in result.stderr or "ConditionalRequestConflict" in result.stderr:
        sys.exit(conflict)
    sys.stderr.write(result.stderr)
    raise subprocess.CalledProcessError(result.returncode, result.args)


def publish_manifest(bucket: str, path: Path, key: str, region: str) -> None:
    """Create the build's manifest, the object that makes a build visible to hydration."""
    print(f"==> uploading s3://{bucket}/{key}")
    conditional_put(
        bucket,
        path,
        key,
        region,
        content_type="application/json",
        precondition=["--if-none-match", "*"],
        conflict=f"refusing to overwrite existing object: s3://{bucket}/{key}",
    )


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


def list_build_objects(bucket: str, prefix: str, region: str) -> dict[str, dict[str, Any]]:
    """Return objects grouped by immutable build id under one image prefix."""
    prefix = f"{prefix}/"
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


def object_etag(bucket: str, key: str, region: str) -> str | None:
    """Return an object's ETag, or None only when the object does not exist."""
    result = subprocess.run(
        aws_argv(region, "s3api", "head-object", "--bucket", bucket, "--key", key),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "An error occurred (404)" in result.stderr:
            return None
        sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return json.loads(result.stdout)["ETag"]


def write_pointer(bucket: str, key: str, body: str, region: str, current: str | None) -> None:
    """Replace the pointer only if it is still *current* (or still absent)."""
    print(f"==> writing pointer s3://{bucket}/{key}")
    precondition = ["--if-match", current] if current is not None else ["--if-none-match", "*"]
    with tempfile.TemporaryDirectory(prefix=".pointer-") as tmp:
        body_path = Path(tmp) / POINTER_NAME
        body_path.write_text(body)
        conditional_put(
            bucket,
            body_path,
            key,
            region,
            content_type="application/json",
            precondition=precondition,
            conflict=f"promoted pointer s3://{bucket}/{key} changed during promotion; re-run to promote against it",
        )


def pointer_body(args: argparse.Namespace, retained_build_ids: list[str]) -> str:
    return (
        json.dumps(
            {
                "build_id": args.build_id,
                "architecture": args.architecture,
                "machine": args.machine,
                "rollback_build_ids": retained_build_ids[1:],
                "source_sha": args.source_sha,
                "ubuntu": args.ubuntu,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    args = parse_args()
    validate_target(args)
    args.source_sha = source_sha()
    if args.preflight:
        print(f"==> upload target valid: {args.architecture} -> s3://{args.bucket}")
        return 0
    root = artifact_dir(args)
    disks, efivars = collect_artifact_files(root)
    image_key = image_prefix(args.architecture, args.ubuntu, args.machine)
    s3_prefix = f"{image_key}/{args.build_id}"
    bundle_key = f"{s3_prefix}/{BUNDLE_NAME}"
    manifest_key = f"{s3_prefix}/{MANIFEST_NAME}"
    pointer_key = f"{image_key}/{POINTER_NAME}"

    print(f"artifact: {root}")
    print(f"target:   s3://{args.bucket}/{s3_prefix}/")
    if args.dry_run:
        manifest = build_manifest(args=args, disks=disks, efivars=efivars, bundle_sha256=None)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        if args.promote:
            print(f"DRY RUN: would write pointer {pointer_key} -> {args.build_id}")
        return 0

    if not shutil.which("aws"):
        sys.exit("required tool not found on PATH: aws")
    tar = find_tar()

    # Fail before a multi-GB upload if either immutable build object exists or
    # cannot be read. The manifest's conditional create guards publication.
    for key in (bundle_key, manifest_key):
        if object_etag(args.bucket, key, args.region) is not None:
            sys.exit(f"refusing to overwrite existing object: s3://{args.bucket}/{key}")

    # GitLab sends SIGTERM on job timeout or cancel; unwind so both subprocesses
    # stop before a partially streamed archive can be published as a build.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    with tempfile.TemporaryDirectory(prefix=".packer-s3-") as tmp:
        tmpdir = Path(tmp)
        manifest_path = tmpdir / MANIFEST_NAME
        digest = upload_bundle(tar, root, [*disks, efivars], args.bucket, bundle_key, args.region)
        manifest = build_manifest(args=args, disks=disks, efivars=efivars, bundle_sha256=digest)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        tag_object(args.bucket, bundle_key, CANDIDATE_STATE, args.region)
        publish_manifest(args.bucket, manifest_path, manifest_key, args.region)
        tag_object(args.bucket, manifest_key, CANDIDATE_STATE, args.region)

    if args.promote:
        prev = object_etag(args.bucket, pointer_key, args.region)
        builds = list_build_objects(args.bucket, image_key, args.region)
        retained = select_retained_builds(builds, args.build_id)
        tag_builds(
            args.bucket,
            builds,
            retained,
            args.region,
            state=RETAINED_STATE,
        )
        write_pointer(args.bucket, pointer_key, pointer_body(args, retained), args.region, prev)
        expirable = [build_id for build_id in builds if build_id not in retained]
        tag_builds(
            args.bucket,
            builds,
            expirable,
            args.region,
            state=EXPIRABLE_STATE,
        )
        print(f"==> rollback builds: {' '.join(retained[1:]) or '(none)'}")
    else:
        print("==> upload complete; promotion pending (re-run with --promote)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

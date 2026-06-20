#!/usr/bin/env python3
# MISE description="Bundle a published packer qemu artifact and upload it to the S3 image bucket"
# USAGE arg "<machine>" help="Packer source/artifact name: box, box_deps, lab, or pug"
# USAGE complete "machine" run="printf 'box\nbox_deps\nlab\npug\n'"
# USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
# USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
# USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
# USAGE flag "--build-id <build_id>" help="Immutable S3 build id; default is timestamp + current git SHA"
# USAGE flag "--artifact-dir <path>" help="Artifact dir to bundle; default is $HOMELAB_CI_DIR/<ubuntu>/<machine>"
# USAGE flag "--mirror-endpoint <url>" help="S3-compatible mirror endpoint (MinIO); enables mirror publish"
# USAGE flag "--mirror-bucket <bucket>" help="Bucket on the mirror endpoint; defaults to --bucket"
# USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build id"
# USAGE flag "--dry-run" help="Build and print the manifest plan without creating the tarball, uploading, or promoting"
"""Upload qemu packer artifacts to the nested-CI S3 bundle layout.

The nested-qemu runner design uses S3 as the source of truth for qemu fixture
images. S3 is the primary store; an optional MinIO mirror is published when
``--mirror-endpoint`` is set so the same bundles are reachable from an
S3-compatible endpoint:

    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/manifest.json
    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/disks.tar.zst

The tarball contains the packer-ubuntu-N.{raw,qcow2} disks plus efivars.fd,
because the qemu harness copies efivars.fd from the same artifact directory
before booting ZFS-root variants. The manifest records the actual disk format
so Linux-built raw images and macOS-built qcow2 images remain explicit.

The live build for each machine/release pair is selected by a pointer object
(not SSM) stored inside the bucket itself:

    s3://<bucket>/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}

so the same code promotes against AWS S3 and a MinIO mirror identically.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_NAME = "disks.tar.zst"
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "promoted.json"
S3_CHECKSUM_ALGORITHM = "SHA256"
VALID_MACHINES = {"box", "box_deps", "lab", "pug"}


@dataclass(frozen=True)
class Target:
    """A publish destination for the bundle/manifest/pointer objects.

    ``endpoint_url`` None selects plain AWS S3 with ambient/OIDC credentials.
    ``creds_env`` None means inherit the process environment as-is; otherwise it
    is a static (access-key, secret-key) pair used for an S3-compatible mirror.
    """

    name: str
    endpoint_url: str | None
    bucket: str
    creds_env: tuple[str, str] | None


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
        "--mirror-endpoint",
        default=os.environ.get("usage_mirror_endpoint") or os.environ.get("HOMELAB_CI_MIRROR_ENDPOINT"),
    )
    parser.add_argument(
        "--mirror-bucket",
        default=os.environ.get("usage_mirror_bucket") or os.environ.get("HOMELAB_CI_MIRROR_BUCKET"),
    )
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


def build_targets(args: argparse.Namespace) -> tuple[Target, Target | None]:
    """Resolve the primary (S3) and optional mirror (MinIO) publish targets.

    The primary always uses ambient/OIDC credentials. The mirror is configured
    only when ``--mirror-endpoint`` is set, and then requires static MinIO keys
    in HOMELAB_CI_MIRROR_ACCESS_KEY / HOMELAB_CI_MIRROR_SECRET_KEY.
    """
    primary = Target(name="s3", endpoint_url=None, bucket=args.bucket, creds_env=None)
    if not args.mirror_endpoint:
        return primary, None
    access = os.environ.get("HOMELAB_CI_MIRROR_ACCESS_KEY")
    secret = os.environ.get("HOMELAB_CI_MIRROR_SECRET_KEY")
    if not access or not secret:
        sys.exit(
            "--mirror-endpoint is set but HOMELAB_CI_MIRROR_ACCESS_KEY / "
            "HOMELAB_CI_MIRROR_SECRET_KEY are not both present in the environment"
        )
    mirror = Target(
        name="mirror",
        endpoint_url=args.mirror_endpoint,
        bucket=args.mirror_bucket or args.bucket,
        creds_env=(access, secret),
    )
    return primary, mirror


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"required tool not found on PATH: {name}")
    return path


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


def file_entry(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def artifact_dir(args: argparse.Namespace) -> Path:
    if args.artifact_dir:
        return Path(args.artifact_dir).expanduser().resolve()
    base = os.environ.get("HOMELAB_CI_DIR")
    if not base:
        base = str(REPO_ROOT / "packer" / "artifacts")
    return (Path(base) / args.ubuntu / args.machine).resolve()


def collect_artifact_files(root: Path) -> tuple[list[Path], list[Path]]:
    if not root.is_dir():
        sys.exit(f"artifact directory does not exist: {root}")
    disks = sorted(root.glob("packer-ubuntu-*.raw")) + sorted(root.glob("packer-ubuntu-*.qcow2"))
    if not disks:
        sys.exit(f"no packer-ubuntu-*.{{raw,qcow2}} disks found in {root}")
    efivars = root / "efivars.fd"
    if not efivars.is_file():
        sys.exit(f"missing efivars.fd in {root}; qemu harness expects it beside the disks")
    return disks, [efivars]


def build_manifest(
    *,
    args: argparse.Namespace,
    root: Path,
    disks: list[Path],
    support_files: list[Path],
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
        "support_files": [file_entry(path) for path in support_files],
        "tar_members": [path.name for path in [*disks, *support_files]],
    }


def create_bundle(tar: str, root: Path, members: list[str], bundle: Path) -> None:
    print(f"==> creating {bundle}")
    run([tar, "--sparse", "--zstd", "-cf", str(bundle), "-C", str(root), *members])


def aws_env(target: Target) -> dict[str, str] | None:
    """Return the subprocess env for a target, or None to inherit unchanged.

    For a mirror with static keys, set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
    and strip the OIDC/profile selectors so the CLI uses the MinIO keys instead
    of assuming the build job's web-identity role.
    """
    if target.creds_env is None:
        return None
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"] = target.creds_env
    for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE"):
        env.pop(key, None)
    return env


def aws_argv(target: Target, region: str, *args: str) -> list[str]:
    argv = ["aws", "--region", region]
    if target.endpoint_url:
        argv += ["--endpoint-url", target.endpoint_url]
    argv += list(args)
    return argv


def aws_run(target: Target, region: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run(aws_argv(target, region, *args), env=aws_env(target), **kwargs)


def object_exists(target: Target, key: str, region: str) -> bool:
    result = subprocess.run(
        aws_argv(target, region, "s3api", "head-object", "--bucket", target.bucket, "--key", key),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=aws_env(target),
    )
    return result.returncode == 0


def assert_new_object(target: Target, key: str, region: str) -> None:
    if object_exists(target, key, region):
        sys.exit(f"refusing to overwrite existing object on {target.name}: s3://{target.bucket}/{key}")


def upload_file(target: Target, path: Path, key: str, region: str, content_type: str | None = None) -> None:
    args = [
        "s3",
        "cp",
        str(path),
        f"s3://{target.bucket}/{key}",
        "--only-show-errors",
        "--checksum-algorithm",
        S3_CHECKSUM_ALGORITHM,
    ]
    if content_type:
        args += ["--content-type", content_type]
    print(f"==> uploading s3://{target.bucket}/{key} ({target.name})")
    aws_run(target, region, *args)


def remove_object(target: Target, key: str, region: str) -> None:
    aws_run(target, region, "s3", "rm", f"s3://{target.bucket}/{key}", "--only-show-errors")


def read_pointer(target: Target, key: str, region: str) -> str | None:
    """Return the raw current pointer body, or None when absent/empty."""
    result = subprocess.run(
        aws_argv(target, region, "s3", "cp", f"s3://{target.bucket}/{key}", "-"),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=aws_env(target),
    )
    if result.returncode != 0:
        return None
    return result.stdout or None


def write_pointer(target: Target, key: str, body: str, region: str) -> None:
    print(f"==> writing pointer s3://{target.bucket}/{key} ({target.name})")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(body)
        tmp_path = fh.name
    try:
        aws_run(
            target,
            region,
            "s3",
            "cp",
            tmp_path,
            f"s3://{target.bucket}/{key}",
            "--only-show-errors",
            "--content-type",
            "application/json",
        )
    finally:
        os.unlink(tmp_path)


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
    primary, mirror = build_targets(args)
    root = artifact_dir(args)
    disks, support_files = collect_artifact_files(root)
    s3_prefix = f"{args.ubuntu}/{args.machine}/{args.build_id}"
    bundle_key = f"{s3_prefix}/{BUNDLE_NAME}"
    manifest_key = f"{s3_prefix}/{MANIFEST_NAME}"
    pointer_key = f"{args.ubuntu}/{args.machine}/{POINTER_NAME}"

    print(f"artifact: {root}")
    print(f"target:   s3://{args.bucket}/{s3_prefix}/")
    if mirror:
        print(f"mirror:   s3://{mirror.bucket}/{s3_prefix}/ via {mirror.endpoint_url}")
    manifest = build_manifest(args=args, root=root, disks=disks, support_files=support_files, s3_prefix=s3_prefix)

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        if mirror:
            print(f"DRY RUN: would mirror to s3://{mirror.bucket}/{s3_prefix}/ via {mirror.endpoint_url}")
        else:
            print("DRY RUN: no mirror configured (set --mirror-endpoint to enable)")
        if args.promote:
            print(f"DRY RUN: would write pointer {pointer_key} -> {args.build_id}")
        return 0

    require_tool("aws")
    tar = find_tar()

    # Immutability: refuse to overwrite an existing build on either target,
    # mirror first so a half-configured mirror fails before any S3 mutation.
    if mirror:
        assert_new_object(mirror, bundle_key, args.region)
        assert_new_object(mirror, manifest_key, args.region)
    assert_new_object(primary, bundle_key, args.region)
    assert_new_object(primary, manifest_key, args.region)

    with tempfile.TemporaryDirectory(prefix="packer-s3-", dir=os.environ.get("TMPDIR")) as tmp:
        tmpdir = Path(tmp)
        bundle = tmpdir / BUNDLE_NAME
        manifest_path = tmpdir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        create_bundle(tar, root, manifest["tar_members"], bundle)

        # MinIO first; if S3 then fails, roll the mirror back so a build never
        # exists on the mirror without its S3 counterpart.
        if mirror:
            upload_file(mirror, bundle, bundle_key, args.region, "application/zstd")
            upload_file(mirror, manifest_path, manifest_key, args.region, "application/json")
        try:
            upload_file(primary, bundle, bundle_key, args.region, "application/zstd")
            upload_file(primary, manifest_path, manifest_key, args.region, "application/json")
        except Exception:
            if mirror:
                print("==> S3 upload failed; rolling back mirror objects", file=sys.stderr)
                for key in (bundle_key, manifest_key):
                    try:
                        remove_object(mirror, key, args.region)
                    except subprocess.CalledProcessError:
                        # Best-effort cleanup: a leftover mirror object is
                        # caught by the next run's assert_new_object.
                        pass
            raise

    if args.promote:
        body = pointer_body(args)
        prev_primary = read_pointer(primary, pointer_key, args.region)
        prev_mirror = read_pointer(mirror, pointer_key, args.region) if mirror else None
        if mirror:
            write_pointer(mirror, pointer_key, body, args.region)
        try:
            write_pointer(primary, pointer_key, body, args.region)
        except Exception:
            if mirror:
                print("==> S3 pointer write failed; restoring mirror pointer", file=sys.stderr)
                if prev_mirror is not None:
                    write_pointer(mirror, pointer_key, prev_mirror, args.region)
                else:
                    try:
                        remove_object(mirror, pointer_key, args.region)
                    except subprocess.CalledProcessError:
                        # Best-effort: no prior pointer existed to restore.
                        pass
            raise
        if prev_primary is not None:
            print(f"==> previous S3 pointer: {prev_primary.strip()}")
    else:
        print("==> upload complete; promotion pending (re-run with --promote)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

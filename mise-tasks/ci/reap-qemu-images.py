#!/usr/bin/env -S uv run --script
# [MISE] description="Prune old qemu image bundles from the CI S3 bucket"
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""Prune old nested-qemu image bundles from S3.

The live image for each machine/release pair is read from the promoted.json
pointer object:

    s3://homelab-ci-images/<ubuntu>/<machine>/promoted.json -> {"build_id": ...}

The bucket layout is:

    s3://homelab-ci-images/<ubuntu>/<machine>/<build-id>/

This task keeps the promoted build, the newest extra builds, and anything
younger than the grace period. It prints the deletion plan by default and only
mutates the bucket when called with --apply. The bucket lifecycle handles
noncurrent versions, so a stale prefix is swept by deleting every object
version.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

DEFAULT_BUCKET = "homelab-ci-images"
DEFAULT_REGION = "eu-central-1"
DEFAULT_MACHINES = ("box", "box_deps")
DEFAULT_UBUNTUS = ("jammy", "noble", "resolute")
POINTER_NAME = "promoted.json"
DELETE_BATCH_SIZE = 1000

CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


@dataclass
class Build:
    ubuntu: str
    machine: str
    build_id: str
    prefix: str
    last_modified: dt.datetime
    object_count: int
    size_bytes: int
    keep_reasons: list[str] = field(default_factory=list)

    @property
    def keep(self) -> bool:
        return bool(self.keep_reasons)


def parse_csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise argparse.ArgumentTypeError("must contain at least one value")
    return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--machines", type=parse_csv, default=DEFAULT_MACHINES)
    parser.add_argument("--ubuntus", type=parse_csv, default=DEFAULT_UBUNTUS)
    parser.add_argument("--keep-newest", type=int, default=3)
    parser.add_argument("--keep-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.keep_newest < 0:
        parser.error("--keep-newest must be >= 0")
    if args.keep_days < 0:
        parser.error("--keep-days must be >= 0")
    return args


def s3_client(region: str):
    return boto3.client("s3", region_name=region, config=CFG)


def promoted_build(s3, bucket: str, machine: str, ubuntu: str) -> str | None:
    key = f"{ubuntu}/{machine}/{POINTER_NAME}"
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        code = e.response["Error"].get("Code", "Error")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "404") or status == 404:
            return None
        raise
    pointer = json.loads(body)
    build_id = pointer.get("build_id")
    return build_id or None


def list_build_ids(s3, bucket: str, machine: str, ubuntu: str) -> list[str]:
    root = f"{ubuntu}/{machine}/"
    build_ids: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=root, Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            prefix = common["Prefix"]
            build_id = prefix.removeprefix(root).strip("/")
            if build_id:
                build_ids.append(build_id)
    return sorted(build_ids)


def build_stats(s3, bucket: str, machine: str, ubuntu: str, build_id: str) -> Build:
    prefix = f"{ubuntu}/{machine}/{build_id}/"
    last_modified: dt.datetime | None = None
    size_bytes = 0
    object_count = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            object_count += 1
            size_bytes += int(obj["Size"])
            modified = obj["LastModified"]
            if last_modified is None or modified > last_modified:
                last_modified = modified
    if last_modified is None:
        last_modified = dt.datetime.fromtimestamp(0, tz=dt.UTC)
    return Build(
        ubuntu=ubuntu,
        machine=machine,
        build_id=build_id,
        prefix=prefix,
        last_modified=last_modified,
        object_count=object_count,
        size_bytes=size_bytes,
    )


def mark_keep_reasons(builds: list[Build], promoted: str | None, keep_newest: int, keep_days: int) -> None:
    now = dt.datetime.now(dt.UTC)
    if promoted:
        for build in builds:
            if build.build_id == promoted:
                build.keep_reasons.append("promoted")
                break

    extras = 0
    for build in builds:
        if build.build_id == promoted:
            continue
        if extras < keep_newest:
            build.keep_reasons.append(f"newest-extra-{extras + 1}")
            extras += 1

    grace = dt.timedelta(days=keep_days)
    for build in builds:
        age = now - build.last_modified
        if age <= grace:
            build.keep_reasons.append(f"younger-than-{keep_days}d")


def iter_versions(s3, bucket: str, prefix: str) -> Iterable[dict[str, str]]:
    """Yield every object version + delete marker under a prefix."""
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for key in ("Versions", "DeleteMarkers"):
            for item in page.get(key, []):
                yield {"Key": item["Key"], "VersionId": item["VersionId"]}


def delete_in_batches(s3, bucket: str, items: Iterable[dict[str, str]]) -> int:
    deleted = 0
    batch: list[dict[str, str]] = []
    for item in items:
        batch.append(item)
        if len(batch) == DELETE_BATCH_SIZE:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
            batch = []
    if batch:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch)
    return deleted


def delete_prefix(s3, bucket: str, prefix: str) -> int:
    return delete_in_batches(s3, bucket, iter_versions(s3, bucket, prefix))


def human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{size}B"
        value /= 1024
    return f"{size}B"


def main() -> int:
    args = parse_args()
    s3 = s3_client(args.region)

    print(
        f"== QEMU image reaper: s3://{args.bucket}/ "
        f"(keep promoted + newest {args.keep_newest} + younger than {args.keep_days}d)"
    )
    if not args.apply:
        print("DRY RUN: pass --apply to delete stale build prefixes")

    stale: list[Build] = []
    kept: list[Build] = []
    for ubuntu in args.ubuntus:
        for machine in args.machines:
            promoted = promoted_build(s3, args.bucket, machine, ubuntu)
            builds = [
                build_stats(s3, args.bucket, machine, ubuntu, build_id)
                for build_id in list_build_ids(s3, args.bucket, machine, ubuntu)
            ]
            builds.sort(key=lambda build: build.last_modified, reverse=True)
            mark_keep_reasons(builds, promoted, args.keep_newest, args.keep_days)
            if promoted and all(build.build_id != promoted for build in builds):
                print(f"WARN: {ubuntu}/{machine} promotes missing build {promoted}", file=sys.stderr)
            if not builds:
                print(f"    {ubuntu}/{machine}: no builds")
                continue
            for build in builds:
                (kept if build.keep else stale).append(build)
                marker = "KEEP" if build.keep else "DROP"
                reason = ",".join(build.keep_reasons) if build.keep else "stale"
                print(
                    f"    {marker} {ubuntu}/{machine}/{build.build_id} "
                    f"{build.last_modified:%Y-%m-%d} "
                    f"{build.object_count} objects {human_size(build.size_bytes)} "
                    f"({reason})"
                )

    print(f"\nSummary: keep {len(kept)} build prefixes, delete {len(stale)} stale build prefixes")
    if not stale:
        return 0
    if not args.apply:
        return 0

    deleted = 0
    for build in stale:
        print(f"==> deleting all versions under s3://{args.bucket}/{build.prefix}")
        deleted += delete_prefix(s3, args.bucket, build.prefix)
    print(f"Deleted {deleted} object versions/delete markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

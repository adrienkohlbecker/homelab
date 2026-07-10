#!/usr/bin/env python3
"""Shared retention policy for homelab-owned AMIs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable

AMI_RETAIN_PER_CATEGORY = 2


def ami_category(image: dict) -> str:
    """Return the stable retention category for an AMI."""
    name_tag = next(
        (tag["Value"] for tag in image.get("Tags", []) if tag["Key"] == "Name"),
        None,
    )
    return name_tag or re.sub(r"-\d+$", "", image.get("Name", image["ImageId"]))


def snapshot_ids(image: dict) -> list[str]:
    """Return the EBS snapshot ids backing an AMI."""
    return [
        mapping["Ebs"]["SnapshotId"]
        for mapping in image.get("BlockDeviceMappings", [])
        if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]
    ]


def retention_plan(
    images: Iterable[dict],
    protected_ids: Iterable[str] = (),
    keep: int = AMI_RETAIN_PER_CATEGORY,
) -> tuple[list[dict], list[dict]]:
    """Split AMIs into retained and stale sets by category and creation date."""
    protected = set(protected_ids)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for image in images:
        by_category[ami_category(image)].append(image)

    retained: list[dict] = []
    stale: list[dict] = []
    for group in by_category.values():
        group.sort(key=lambda image: image["CreationDate"], reverse=True)
        retained_ids = {image["ImageId"] for image in group[:keep]} | protected
        for image in group:
            (retained if image["ImageId"] in retained_ids else stale).append(image)
    return retained, stale


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protected", action="append", default=[])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _, stale = retention_plan(json.load(sys.stdin), args.protected)
    for image in stale:
        print(image["ImageId"])


if __name__ == "__main__":
    main()

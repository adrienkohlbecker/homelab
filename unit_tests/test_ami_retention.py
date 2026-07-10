from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "ami_retention.py"
_SPEC = importlib.util.spec_from_file_location("ami_retention", _MODULE_PATH)
assert _SPEC
assert _SPEC.loader
ami_retention = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ami_retention)


def image(image_id: str, created: str, *, name: str = "qemu-host") -> dict:
    return {
        "ImageId": image_id,
        "CreationDate": created,
        "Tags": [{"Key": "Name", "Value": name}],
        "BlockDeviceMappings": [],
    }


def test_retention_keeps_newest_and_older_protected_image() -> None:
    images = [
        image("ami-new", "2026-07-03"),
        image("ami-second", "2026-07-02"),
        image("ami-promoted", "2026-07-01"),
        image("ami-stale", "2026-06-30"),
    ]

    retained, stale = ami_retention.retention_plan(images, {"ami-promoted"})

    assert [item["ImageId"] for item in retained] == [
        "ami-new",
        "ami-second",
        "ami-promoted",
    ]
    assert [item["ImageId"] for item in stale] == ["ami-stale"]


def test_retention_is_independent_per_category() -> None:
    images = [
        image("ami-a-new", "2026-07-03", name="a"),
        image("ami-a-old", "2026-07-01", name="a"),
        image("ami-b-new", "2026-07-04", name="b"),
        image("ami-b-old", "2026-06-30", name="b"),
    ]

    retained, stale = ami_retention.retention_plan(images, keep=1)

    assert {item["ImageId"] for item in retained} == {"ami-a-new", "ami-b-new"}
    assert {item["ImageId"] for item in stale} == {"ami-a-old", "ami-b-old"}


def test_category_falls_back_to_timestamp_free_ami_name() -> None:
    untagged = {
        "ImageId": "ami-1",
        "Name": "homelab-ci-qemu-host-noble-1780000000",
    }

    assert ami_retention.ami_category(untagged) == "homelab-ci-qemu-host-noble"


def test_snapshot_ids_ignores_non_ebs_mappings() -> None:
    value = {
        "BlockDeviceMappings": [
            {"Ebs": {"SnapshotId": "snap-1"}},
            {"VirtualName": "ephemeral0"},
            {"Ebs": {"DeleteOnTermination": True}},
        ]
    }

    assert ami_retention.snapshot_ids(value) == ["snap-1"]

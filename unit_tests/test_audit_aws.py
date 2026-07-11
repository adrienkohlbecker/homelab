import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import EndpointConnectionError

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "audit-aws.py"
_MODULE_DIR = str(_MODULE_PATH.parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)
_SPEC = importlib.util.spec_from_file_location("audit_aws", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
audit_aws = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_aws)


def image(image_id, *, tags, snapshot_id):
    return {
        "ImageId": image_id,
        "CreationDate": "2026-07-10T00:00:00Z",
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
        "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot_id}}],
    }


def reset_output():
    audit_aws.anomalies.clear()
    audit_aws.deletes.clear()
    audit_aws.expected.clear()
    audit_aws.errors.clear()


def test_unknown_ami_is_reported_without_cleanup(monkeypatch):
    reset_output()
    unknown = image(
        "ami-unknown",
        tags={"Name": "unrecognized"},
        snapshot_id="snap-referenced",
    )
    snapshots = [
        {
            "SnapshotId": "snap-referenced",
            "VolumeSize": 8,
            "StartTime": datetime(2026, 7, 10, tzinfo=UTC),
        }
    ]
    monkeypatch.setattr(audit_aws, "promoted_qemu_host_amis", lambda region, images: set())

    audit_aws.audit_ami_inventory("eu-central-1", [unknown], snapshots)

    expected = "[eu-central-1] unexpected AMI ami-unknown (unrecognized, 2026-07-10) " "-- manual review required"
    assert audit_aws.anomalies == [expected]
    assert audit_aws.deletes == []


def test_incomplete_inventory_emits_no_classification():
    reset_output()

    audit_aws.audit_ami_inventory("eu-central-1", [], None)

    assert audit_aws.anomalies == []
    assert audit_aws.deletes == []
    assert audit_aws.expected == []


def test_paginated_collects_every_page():
    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"Owners": ["self"]}
            return [{"Images": [{"ImageId": "ami-1"}]}, {"Images": [{"ImageId": "ami-2"}]}]

    class Service:
        def get_paginator(self, operation):
            assert operation == "describe_images"
            return Paginator()

    reset_output()

    result = audit_aws.paginated(
        "images",
        Service(),
        "describe_images",
        "Images",
        Owners=["self"],
    )

    assert result == [{"ImageId": "ami-1"}, {"ImageId": "ami-2"}]
    assert audit_aws.errors == []


def test_safe_records_transport_errors():
    reset_output()

    result = audit_aws.safe(
        "snapshots",
        lambda: (_ for _ in ()).throw(EndpointConnectionError(endpoint_url="https://ec2.invalid")),
        default=None,
    )

    assert result is None
    assert len(audit_aws.errors) == 1
    assert audit_aws.errors[0].startswith("snapshots: EndpointConnectionError:")


def test_retry_budget_counts_total_attempts():
    assert audit_aws.CFG.retries == {"total_max_attempts": 10, "mode": "adaptive"}

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import EndpointConnectionError
from conftest import load_repo_module

_MODULE_PATH = Path(__file__).parents[1] / "mise-tasks" / "ci" / "audit-aws.py"
_MODULE_DIR = str(_MODULE_PATH.parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)
audit_aws = load_repo_module("mise-tasks/ci/audit-aws.py", name="audit_aws")


def image(image_id, *, tags, snapshot_id, architecture="x86_64"):
    return {
        "ImageId": image_id,
        "Architecture": architecture,
        "CreationDate": "2026-07-10T00:00:00Z",
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
        "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot_id}}],
    }


@pytest.fixture(autouse=True)
def reset_output():
    audit_aws.anomalies.clear()
    audit_aws.deletes.clear()
    audit_aws.expected.clear()
    audit_aws.errors.clear()


def test_unknown_ami_is_reported_without_cleanup(monkeypatch):
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

    expected = "[eu-central-1] unexpected AMI ami-unknown (unrecognized, 2026-07-10) -- manual review required"
    assert audit_aws.anomalies == [expected]
    assert audit_aws.deletes == []


def test_report_omits_empty_cleanup_section(monkeypatch, capsys):
    class Sts:
        @staticmethod
        def get_caller_identity():
            return {"Account": "123", "Arn": "test-role"}

    audit_aws.anomalies.append("manual review only")
    monkeypatch.setattr(audit_aws, "client", lambda *_args: Sts())
    monkeypatch.setattr(audit_aws, "safe", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(audit_aws, "paginated", lambda *_args, **_kwargs: [])

    with pytest.raises(SystemExit, match="1"):
        audit_aws.main()

    output = capsys.readouterr().out
    assert "manual review only" in output
    assert "Suggested cleanup" not in output


def test_incomplete_inventory_emits_no_classification():
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


def test_supported_qemu_images_are_region_and_architecture_specific():
    x86_tags = {
        "Name": "homelab-ci-qemu-host-noble",
        "architecture": "x86_64",
        "machine": "qemu_host",
        "role": "ci-ami",
        "ubuntu": "noble",
    }
    x86 = image("ami-x86", tags=x86_tags, snapshot_id="snap-x86")
    unlabelled_x86 = image(
        "ami-x86-unlabelled",
        tags={key: value for key, value in x86_tags.items() if key != "architecture"},
        snapshot_id="snap-x86-unlabelled",
    )
    arm = image(
        "ami-arm",
        tags={
            "Name": "homelab-ci-qemu-host-aarch64-noble",
            "architecture": "aarch64",
            "machine": "qemu_host",
            "role": "ci-ami",
            "ubuntu": "noble",
        },
        snapshot_id="snap-arm",
        architecture="arm64",
    )

    assert audit_aws.is_supported_qemu_host_image("eu-central-1", x86)
    assert audit_aws.is_supported_qemu_host_image("eu-central-1", arm)
    assert not audit_aws.is_supported_qemu_host_image("eu-west-1", arm)
    assert not audit_aws.is_supported_qemu_host_image("eu-west-1", x86)
    # Older x86 AMIs lack the architecture tag but retain the EC2 architecture.
    assert audit_aws.is_supported_qemu_host_image("eu-central-1", unlabelled_x86)
    assert not audit_aws.is_supported_qemu_host_image("eu-central-1", {**unlabelled_x86, "Architecture": "arm64"})


def test_ami_recognition_follows_the_shared_architecture_table():
    architecture_table = yaml.safe_load((Path(__file__).parents[1] / "data/architectures.yml").read_text())
    contract = audit_aws.CI_REGIONS["eu-central-1"]
    assert {"homelab-ci-images"} == audit_aws.EXPECTED_GLOBAL_S3_BUCKETS
    assert contract["asgs"] == {"homelab-ci-qemu-host", "homelab-ci-qemu-arm"}
    for architecture, ec2_architecture in (("x86_64", "x86_64"), ("aarch64", "arm64")):
        ci = architecture_table[architecture]["ci"]
        assert contract["amis"][ec2_architecture] == {
            "name": f"{ci['ami_name_prefix']}-noble",
            "parameter": ci["ami_parameter"],
        }


def test_active_asg_instances_are_not_orphans():
    audit_aws.audit_compute_inventory(
        "eu-central-1",
        [{"AutoScalingGroupName": "homelab-ci-qemu-arm", "Instances": [{"InstanceId": "i-active"}]}],
        [{"Instances": [{"InstanceId": "i-active", "InstanceType": "c7gd.metal", "State": {"Name": "running"}}]}],
    )

    assert audit_aws.anomalies == []


def test_stray_asg_and_instances_are_orphans():
    audit_aws.audit_compute_inventory(
        "eu-central-1",
        [{"AutoScalingGroupName": "homelab-ci-stale", "Instances": [{"InstanceId": "i-stray"}]}],
        [{"Instances": [{"InstanceId": "i-stray", "InstanceType": "c8id.4xlarge", "State": {"Name": "running"}}]}],
    )

    assert audit_aws.anomalies == [
        "[eu-central-1] orphan Auto Scaling group homelab-ci-stale",
        "[eu-central-1] orphan EC2 instance i-stray (c8id.4xlarge, running)",
    ]


def test_unknown_asg_inventory_does_not_classify_instances():
    audit_aws.audit_compute_inventory(
        "eu-central-1",
        None,
        [{"Instances": [{"InstanceId": "i-active", "InstanceType": "c7gd.metal", "State": {"Name": "running"}}]}],
    )

    assert audit_aws.anomalies == []


def test_sweep_reports_only_unattached_volumes_and_addresses(monkeypatch):
    class Ec2:
        @staticmethod
        def describe_addresses():
            return {
                "Addresses": [
                    {"PublicIp": "192.0.2.1", "AssociationId": "eipassoc-1"},
                    {"PublicIp": "192.0.2.2"},
                ]
            }

    inventory = {
        "describe_auto_scaling_groups": [],
        "describe_instances": [],
        "describe_volumes": [
            {"VolumeId": "vol-attached", "Size": 40, "State": "in-use"},
            {"VolumeId": "vol-orphan", "Size": 40, "State": "available"},
        ],
    }
    monkeypatch.setattr(audit_aws, "client", lambda service, _region: Ec2() if service == "ec2" else object())
    monkeypatch.setattr(
        audit_aws,
        "paginated",
        lambda _label, _service, operation, _key, **_kwargs: inventory.get(operation, []),
    )

    audit_aws.sweep_region("eu-central-1")

    assert audit_aws.anomalies == [
        "[eu-central-1] unattached EBS volume vol-orphan (40GB)",
        "[eu-central-1] unassociated Elastic IP 192.0.2.2",
    ]

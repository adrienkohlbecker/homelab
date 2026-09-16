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


def image(image_id, *, tags, snapshot_id):
    return {
        "ImageId": image_id,
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
    )

    assert audit_aws.is_supported_qemu_host_image("eu-central-1", x86)
    assert audit_aws.is_supported_qemu_host_image("eu-central-1", arm)
    assert not audit_aws.is_supported_qemu_host_image("eu-west-1", arm)
    assert not audit_aws.is_supported_qemu_host_image("eu-west-1", x86)
    # Every retention-managed AMI carries its architecture tag.
    assert not audit_aws.is_supported_qemu_host_image("eu-central-1", unlabelled_x86)


def test_region_contracts_follow_the_shared_architecture_table():
    architecture_table = yaml.safe_load((Path(__file__).parents[1] / "data/architectures.yml").read_text())
    assert {entry["ci"]["aws_region"] for entry in architecture_table.values()} == {"eu-central-1"}
    contract = audit_aws.CI_REGIONS["eu-central-1"]
    assert contract["buckets"] == {entry["ci"]["image_bucket"] for entry in architecture_table.values()}
    for architecture, ec2_architecture in (("x86_64", "x86_64"), ("aarch64", "arm64")):
        ci = architecture_table[architecture]["ci"]
        assert contract["amis"][ec2_architecture] == {
            "name": f"{ci['ami_name_prefix']}-noble",
            "parameter": ci["ami_parameter"],
        }


def _spot_asg(name: str, maximum: int) -> dict:
    group = {
        "AutoScalingGroupName": name,
        "MinSize": 0,
        "MaxSize": maximum,
        "NewInstancesProtectedFromScaleIn": True,
        "MixedInstancesPolicy": {
            "InstancesDistribution": {
                "OnDemandBaseCapacity": 0,
                "OnDemandPercentageAboveBaseCapacity": 0,
                "SpotAllocationStrategy": "price-capacity-optimized",
            }
        },
    }
    if name == "homelab-ci-qemu-arm":
        group["AvailabilityZones"] = sorted(audit_aws.ARM_AVAILABILITY_ZONES)
        group["MixedInstancesPolicy"]["LaunchTemplate"] = {
            "Overrides": [{"InstanceType": instance_type} for instance_type in sorted(audit_aws.ARM_INSTANCE_TYPES)]
        }
    return group


def _arm_instance_type(instance_type: str) -> dict:
    return {
        "InstanceType": instance_type,
        "VCpuInfo": {"DefaultVCpus": 64},
        "MemoryInfo": {"SizeInMiB": 128 * 1024},
        "InstanceStorageSupported": True,
        "InstanceStorageInfo": {
            "TotalSizeInGB": 3800,
            "Disks": [{"Count": 2, "SizeInGB": 1900, "Type": "ssd"}],
        },
    }


def test_frankfurt_contract_documents_are_accepted():
    audit_aws.audit_asg_documents(
        "eu-central-1",
        [
            _spot_asg("homelab-ci-qemu-host", 5),
            _spot_asg("homelab-ci-qemu-site", 1),
            _spot_asg("homelab-ci-qemu-arm", 1),
        ],
    )
    audit_aws.audit_arm_capacity_documents(
        {"Value": 160.0},
        [
            {"InstanceType": "c6gd.metal", "Location": "eu-central-1a"},
            {"InstanceType": "c7gd.metal", "Location": "eu-central-1b"},
            {"InstanceType": "m6gd.metal", "Location": "eu-central-1c"},
            {"InstanceType": "m7gd.metal", "Location": "eu-central-1a"},
        ],
        [_arm_instance_type(instance_type) for instance_type in sorted(audit_aws.ARM_INSTANCE_TYPES)],
    )
    audit_aws.audit_promoted_image_document(
        "eu-central-1",
        "ami-arm",
        [{"ImageId": "ami-arm", "Architecture": "arm64"}],
        "arm64",
    )
    audit_aws.audit_guard_documents(
        "eu-central-1",
        {"ImageBlockPublicAccessState": "block-new-sharing"},
        {"State": "block-all-sharing"},
        {"AccountLevel": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1}},
    )
    audit_aws.audit_ecr_documents(
        "eu-central-1",
        [
            {
                "ecrRepositoryPrefix": prefix,
                "upstreamRegistryUrl": upstream,
                **(
                    {"credentialArn": f"arn:aws:secretsmanager:eu-central-1:123:secret:{prefix}"}
                    if prefix != "quay"
                    else {}
                ),
            }
            for prefix, upstream in audit_aws.ECR_UPSTREAMS.items()
        ],
        [{"repositoryName": "docker-hub/library/ubuntu"}],
    )
    audit_aws.audit_bucket_documents(
        "eu-central-1",
        "homelab-ci-arm-images-eu-central-1",
        {"LocationConstraint": "eu-central-1"},
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
        {"Status": "Enabled"},
        {
            "Statement": [
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
                {
                    "Sid": "DenyCrossAccountAccess",
                    "Effect": "Deny",
                    "Condition": {"StringNotEquals": {"aws:PrincipalAccount": "000390721279"}},
                },
            ]
        },
    )

    assert audit_aws.anomalies == []


def test_frankfurt_contract_mismatches_are_reported():
    audit_aws.audit_asg_documents(
        "eu-central-1",
        [
            _spot_asg("homelab-ci-qemu-arm", 2),
            _spot_asg("homelab-ci-stale", 1),
        ],
    )
    audit_aws.audit_arm_capacity_documents(
        {"Value": 32.0},
        [],
        [],
    )
    audit_aws.audit_promoted_image_document(
        "eu-central-1",
        "ami-wrong",
        [{"ImageId": "ami-wrong", "Architecture": "x86_64"}],
        "arm64",
    )
    audit_aws.audit_guard_documents("eu-central-1", {}, {}, {})
    audit_aws.audit_ecr_documents("eu-central-1", [], [{"repositoryName": "unexpected/repo"}])
    audit_aws.audit_bucket_documents("eu-central-1", "homelab-ci-arm-images-eu-central-1", {}, {}, {}, {})

    output = "\n".join(audit_aws.anomalies)
    for message in (
        "ASG homelab-ci-qemu-arm bounds",
        "unexpected CI Auto Scaling group homelab-ci-stale",
        "Standard Spot quota",
        "c6gd.metal is unavailable",
        "c7gd.metal is unavailable",
        "m6gd.metal is unavailable",
        "m7gd.metal is unavailable",
        "promoted qemu-host AMI ami-wrong architecture",
        "AMI public-access block",
        "ECR pull-through rules differ",
        "unexpected ECR repository",
        "public-access block is incomplete",
    ):
        assert message in output

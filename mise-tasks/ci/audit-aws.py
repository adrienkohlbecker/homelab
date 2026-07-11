#!/usr/bin/env -S uv run --script
# [MISE] description="Read-only audit of the CI AWS account for unexpected billable resources"
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""Read-only audit of the homelab CI AWS account (notes/ci_aws_nested_qemu_cells.md).

The account exists solely for the eu-central-1 AWS test-cell pipeline, whose
only standing footprint is meant to be: the nested-qemu host AMI and its
backing snapshot, the qemu image bundle bucket, plus free/free-tier scaffolding
(VPC, SG, launch template, IAM role, SSM params, key pair, EventBridge
Scheduler). The runner hosts are autoscaled spot instances that scale to zero
and the cells run as qemu guests inside them, so *nothing* compute-shaped
should ever be running idle, and no resource of any kind should exist outside
eu-central-1.

This sweeps every region for the billable strays that accumulate when a build
or teardown leaks something -- running/stopped instances, unattached volumes,
Elastic IPs, NAT gateways, VPC interface endpoints, load balancers, RDS -- and
cross-references owned snapshots against owned AMIs to surface *orphaned*
snapshots (a snapshot not backing any live AMI, the classic
deregister/interrupted-packer leftover). Account-global S3 is checked too; the
qemu image bundle bucket is expected, while any other bucket is drift.

Owned AMIs are also held to a retention rule: the promoted qemu-host image plus
the newest AMI_RETAIN_PER_CATEGORY supported builds are legitimate. Only
provenance-tagged noble qemu-host images in eu-central-1 are eligible for
automatic cleanup. Any other owned AMI is reported for manual review.

It NEVER mutates. For each stray AMI it prints the repository's guarded
deregistration task; orphan snapshots still get an exact AWS deletion command.

Exposed as ci:audit-aws. Exits 1 if any anomaly is found, 0 when clean, so it
can double as a periodic check.
"""

import sys
from typing import Any

import boto3
from ami_retention import (
    AMI_RETAIN_PER_CATEGORY,
    ami_category,
    retention_plan,
    snapshot_ids,
)
from botocore.config import Config
from botocore.exceptions import ClientError

# Adaptive retries with a deep attempt budget: a naive fan-out across ~17
# regions throttles, and a throttled describe that silently returns empty would
# read as "no resources" -- exactly the false-clean an audit must avoid.
CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
EXPECTED_GLOBAL_S3_BUCKETS = {"homelab-ci-images"}
anomalies: list[str] = []  # human-readable lines, one per unexpected resource
deletes: list[str] = []  # suggested cleanup commands (never executed here)
expected: list[str] = []  # legitimate standing infra, reported for context
# per-call failures, so a denied/throttled query is never mistaken for empty
errors: list[str] = []
_DEFAULT: Any = object()


def client(svc, region):
    return boto3.client(svc, region_name=region, config=CFG)


def safe(label, fn, default=_DEFAULT) -> Any:
    """Run a describe call, recording (not raising) any failure so the sweep
    finishes and the operator sees which queries could not be trusted."""
    try:
        return fn()
    except ClientError as e:
        errors.append(f"{label}: {e.response['Error'].get('Code', 'Error')}")
        return [] if default is _DEFAULT else default


def image_tags(image: dict) -> dict[str, str]:
    """Return an AMI's tags as a key-value mapping."""
    return {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}


def is_supported_qemu_host_image(region: str, image: dict) -> bool:
    """Return whether an AMI is eligible for automatic retention cleanup."""
    tags = image_tags(image)
    return region == "eu-central-1" and all(
        tags.get(key) == value
        for key, value in {
            "Name": "homelab-ci-qemu-host-noble",
            "role": "ci-ami",
            "machine": "qemu_host",
            "ubuntu": "noble",
        }.items()
    )


def promoted_qemu_host_amis(region: str, images: list[dict]) -> set[str] | None:
    """Return qemu-host AMIs protected by their SSM promotion pointers."""
    if region != "eu-central-1":
        return set()

    ubuntus = {image_tags(image)["ubuntu"] for image in images}
    promoted: set[str] = set()
    ssm = client("ssm", region)
    for ubuntu in ubuntus:
        parameter = f"/homelab-ci/ami/qemu-host/{ubuntu}"
        try:
            value = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        except ClientError as error:
            code = error.response["Error"].get("Code", "Error")
            if code == "ParameterNotFound":
                continue
            errors.append(f"{region} promoted qemu-host {ubuntu}: {code}")
            return None
        promoted.add(value)
    return promoted


def audit_ami_inventory(
    region: str,
    images: list[dict] | None,
    snaps: list[dict] | None,
) -> None:
    """Classify owned AMIs and snapshots when both inventories are trusted."""
    if images is None or snaps is None:
        return

    supported = [image for image in images if is_supported_qemu_host_image(region, image)]
    unsupported = [image for image in images if image not in supported]
    promoted = promoted_qemu_host_amis(region, supported)
    if promoted is None:
        return

    retained, strays = retention_plan(supported, promoted)
    all_referenced = {sid for image in images for sid in snapshot_ids(image)}

    if images or snaps:
        expected.append(
            f"[{region}] {len(retained)} retained AMIs + "
            f"{len({sid for image in retained for sid in snapshot_ids(image)})} backing snapshots"
        )

    anomalies.extend(
        (
            f"[{region}] unexpected AMI {image['ImageId']} "
            f"({ami_category(image)}, {image['CreationDate'][:10]}) -- manual review required"
        )
        for image in unsupported
    )

    for image in strays:
        anomalies.append(
            f"[{region}] stray AMI {image['ImageId']} "
            f"({ami_category(image)}, {image['CreationDate'][:10]}) "
            f"-- beyond newest {AMI_RETAIN_PER_CATEGORY} supported builds"
        )
        deletes.append(f"mise run packer:deregister-ami -- {image['ImageId']} {region}")

    for snap in snaps:
        if snap["SnapshotId"] in all_referenced:
            continue
        name = next((tag["Value"] for tag in snap.get("Tags", []) if tag["Key"] == "Name"), "")
        anomalies.append(
            f"[{region}] orphan snapshot {snap['SnapshotId']} "
            f"({snap['VolumeSize']}GB, {snap['StartTime']:%Y-%m-%d}, "
            f"{name or snap.get('Description', '')[:40]!r}) -- backs no AMI"
        )
        deletes.append(f"aws ec2 delete-snapshot --region {region} --snapshot-id {snap['SnapshotId']}")


def sweep_region(region):
    ec2 = client("ec2", region)

    # ── Compute-shaped strays (should be none -- cells are one-time spot) ──
    anomalies.extend(
        f"[{region}] EC2 instance {i['InstanceId']} ({i['InstanceType']}, {i['State']['Name']})"
        for resv in safe(
            f"{region} instances",
            lambda: ec2.describe_instances().get("Reservations", []),
        )
        for i in resv.get("Instances", [])
        if i["State"]["Name"] != "terminated"
    )

    anomalies.extend(
        f"[{region}] EBS volume {v['VolumeId']} ({v['Size']}GB, {v['State']})"
        for v in safe(f"{region} volumes", lambda: ec2.describe_volumes().get("Volumes", []))
    )

    for a in safe(f"{region} addresses", lambda: ec2.describe_addresses().get("Addresses", [])):
        assoc = a.get("InstanceId") or a.get("AssociationId") or "UNASSOCIATED"
        anomalies.append(f"[{region}] Elastic IP {a['PublicIp']} ({assoc})")

    anomalies.extend(
        f"[{region}] NAT gateway {n['NatGatewayId']} ({n['State']})"
        for n in safe(
            f"{region} nat",
            lambda: ec2.describe_nat_gateways().get("NatGateways", []),
        )
        if n["State"] != "deleted"
    )

    endpoints = safe(
        f"{region} vpc-endpoints",
        lambda: ec2.describe_vpc_endpoints().get("VpcEndpoints", []),
    )
    # Only interface endpoints bill (hourly + data); gateway endpoints (S3
    # /DynamoDB) are free, so they are not flagged.
    anomalies.extend(
        f"[{region}] VPC interface endpoint {e['VpcEndpointId']} ({e['ServiceName']})"
        for e in endpoints
        if e["VpcEndpointType"] == "Interface"
    )

    anomalies.extend(
        f"[{region}] load balancer {lb['LoadBalancerName']} ({lb['Type']})"
        for lb in safe(
            f"{region} elbv2",
            lambda: (client("elbv2", region).describe_load_balancers().get("LoadBalancers", [])),
        )
    )

    anomalies.extend(
        f"[{region}] classic ELB {lb['LoadBalancerName']}"
        for lb in safe(
            f"{region} elb-classic",
            lambda: (client("elb", region).describe_load_balancers().get("LoadBalancerDescriptions", [])),
        )
    )

    anomalies.extend(
        f"[{region}] RDS instance {db['DBInstanceIdentifier']} ({db['DBInstanceClass']})"
        for db in safe(
            f"{region} rds",
            lambda: (client("rds", region).describe_db_instances().get("DBInstances", [])),
        )
    )

    # ── AMIs + snapshots: distinguish legitimate cell images from orphans ──
    images = safe(
        f"{region} images",
        lambda: ec2.describe_images(Owners=["self"]).get("Images", []),
        default=None,
    )
    snaps = safe(
        f"{region} snapshots",
        lambda: ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", []),
        default=None,
    )
    audit_ami_inventory(region, images, snaps)


def main():
    ident = client("sts", "eu-central-1").get_caller_identity()
    print(f"== AWS CI account audit — account {ident['Account']} as {ident['Arn']} ==\n")

    regions = [r["RegionName"] for r in client("ec2", "eu-central-1").describe_regions()["Regions"]]
    print(f"sweeping {len(regions)} regions for billable strays + orphaned snapshots...")
    for region in regions:
        sweep_region(region)

    # Account-global: S3 (terraform state is in MinIO; only CI image bundles
    # live in AWS S3).
    for b in safe("s3", lambda: client("s3", "eu-central-1").list_buckets().get("Buckets", [])):
        if b["Name"] in EXPECTED_GLOBAL_S3_BUCKETS:
            expected.append(f"[global] S3 bucket {b['Name']}")
        else:
            anomalies.append(f"[global] S3 bucket {b['Name']}")

    print("\n── Expected CI infra ──")
    print("\n".join(f"  {line}" for line in expected) or "  (none)")

    if errors:
        print("\n── Query errors (results below may be incomplete) ──")
        print("\n".join(f"  {e}" for e in errors))

    print("\n── Anomalies (billable / unexpected) ──")
    if anomalies:
        print("\n".join(f"  {line}" for line in anomalies))
        print("\n── Suggested cleanup (review, then run by hand — NOT executed) ──")
        print("\n".join(f"  {cmd}" for cmd in deletes))
    else:
        print("  none — account holds only the expected CI infra")

    verdict = len(anomalies)
    print(f"\nVerdict: {verdict} anomal{'y' if verdict == 1 else 'ies'}")
    # Query errors also fail the run: an audit that could not see everything
    # must not report a clean bill of health.
    sys.exit(1 if anomalies or errors else 0)


if __name__ == "__main__":
    main()

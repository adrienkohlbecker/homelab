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

It NEVER mutates. For each orphaned snapshot it prints the exact
`aws ec2 delete-snapshot` line for the operator to review and run by hand.

Exposed as ci:audit-aws. Exits 1 if any anomaly is found, 0 when clean, so it
can double as a periodic check.
"""

import sys

import boto3
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


def client(svc, region):
    return boto3.client(svc, region_name=region, config=CFG)


def safe(label, fn, default=None):
    """Run a describe call, recording (not raising) any failure so the sweep
    finishes and the operator sees which queries could not be trusted."""
    try:
        return fn()
    except ClientError as e:
        errors.append(f"{label}: {e.response['Error'].get('Code', 'Error')}")
        return default if default is not None else []


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
            lambda: client("elbv2", region).describe_load_balancers().get("LoadBalancers", []),
        )
    )

    anomalies.extend(
        f"[{region}] classic ELB {lb['LoadBalancerName']}"
        for lb in safe(
            f"{region} elb-classic",
            lambda: client("elb", region).describe_load_balancers().get("LoadBalancerDescriptions", []),
        )
    )

    anomalies.extend(
        f"[{region}] RDS instance {db['DBInstanceIdentifier']} ({db['DBInstanceClass']})"
        for db in safe(
            f"{region} rds",
            lambda: client("rds", region).describe_db_instances().get("DBInstances", []),
        )
    )

    # ── AMIs + snapshots: distinguish legitimate cell images from orphans ──
    images = safe(
        f"{region} images",
        lambda: ec2.describe_images(Owners=["self"]).get("Images", []),
    )
    snaps = safe(
        f"{region} snapshots",
        lambda: ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", []),
    )

    referenced = {
        bdm["Ebs"]["SnapshotId"]
        for im in images
        for bdm in im.get("BlockDeviceMappings", [])
        if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]
    }
    if images or snaps:
        expected.append(f"[{region}] {len(images)} owned AMIs + {len(referenced)} backing snapshots")
    for s in snaps:
        if s["SnapshotId"] not in referenced:
            name = next((t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), "")
            anomalies.append(
                f"[{region}] orphan snapshot {s['SnapshotId']} "
                f"({s['VolumeSize']}GB, {s['StartTime']:%Y-%m-%d}, "
                f"{name or s.get('Description', '')[:40]!r}) -- backs no AMI"
            )
            deletes.append(f"aws ec2 delete-snapshot --region {region} --snapshot-id {s['SnapshotId']}")


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

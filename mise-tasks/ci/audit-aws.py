#!/usr/bin/env -S uv run --script
# [MISE] description="Read-only audit of the CI AWS account for unexpected billable resources"
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""Read-only audit of the homelab CI AWS account.

The account exists solely for the Frankfurt x86_64 and aarch64 AWS test-cell
pools documented in notes/ci_aws_nested_qemu_cells.md and
notes/ci_aws_arm_qemu_cells.md. Runner hosts autoscale to zero; their regional
VPC, image bucket, ECR cache, AMI pointer, guards, and launch configuration are
the expected standing footprint.

This sweeps every region for the billable strays that accumulate when a build
or teardown leaks something -- running/stopped instances, unattached volumes,
Elastic IPs, NAT gateways, VPC interface endpoints, load balancers, RDS -- and
cross-references owned snapshots against owned AMIs to surface *orphaned*
snapshots (a snapshot not backing any live AMI, the classic
deregister/interrupted-packer leftover). Account-global S3 is checked too; the
two qemu image bundle buckets are expected, while any other bucket is drift.

Owned AMIs are also held to a retention rule: the promoted qemu-host image plus
the newest AMI_RETAIN_PER_CATEGORY supported builds are legitimate. Only
region- and architecture-matching qemu-host images are eligible for automatic
cleanup. Any other owned AMI is reported for manual review.

It NEVER mutates. For each stray AMI it prints the repository's guarded
deregistration task; orphan snapshots still get an exact AWS deletion command.

Exposed as ci:audit-aws. Exits 1 if any anomaly is found, 0 when clean, so it
can double as a periodic check.
"""

import json
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
from botocore.exceptions import BotoCoreError, ClientError

# Adaptive retries with a deep attempt budget: a naive fan-out across ~17
# regions throttles, and a throttled describe that silently returns empty would
# read as "no resources" -- exactly the false-clean an audit must avoid.
CFG = Config(retries={"total_max_attempts": 10, "mode": "adaptive"})
CI_REGIONS: dict[str, dict[str, Any]] = {
    "eu-central-1": {
        "amis": {
            "x86_64": {
                "name": "homelab-ci-qemu-host-noble",
                "parameter": "/homelab-ci/ami/qemu-host/{ubuntu}",
            },
            "arm64": {
                "name": "homelab-ci-qemu-host-aarch64-noble",
                "parameter": "/homelab-ci/ami/qemu-host/aarch64/{ubuntu}",
            },
        },
        "asg_maxima": {
            "homelab-ci-qemu-host": 5,
            "homelab-ci-qemu-site": 1,
            "homelab-ci-qemu-arm": 1,
        },
        "buckets": {"homelab-ci-images", "homelab-ci-arm-images-eu-central-1"},
    },
}
EXPECTED_GLOBAL_S3_BUCKETS = set().union(*(contract["buckets"] for contract in CI_REGIONS.values()))
ECR_UPSTREAMS = {
    "docker-hub": "registry-1.docker.io",
    "github": "ghcr.io",
    "gitlab": "registry.gitlab.com",
    "quay": "quay.io",
}
ARM_INSTANCE_TYPES = {"c6gd.metal", "c7gd.metal", "m6gd.metal", "m7gd.metal"}
ARM_AVAILABILITY_ZONES = {"eu-central-1a", "eu-central-1b", "eu-central-1c"}
STANDARD_SPOT_QUOTA_CODE = "L-34B43A08"
ARM_REQUIRED_SPOT_VCPUS = 152
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
    except BotoCoreError as e:
        errors.append(f"{label}: {type(e).__name__}: {e}")
        return [] if default is _DEFAULT else default


def paginated(label, service, operation, result_key, *, default=_DEFAULT, **kwargs) -> Any:
    """Collect one result list across every page of an AWS operation."""

    def collect():
        paginator = service.get_paginator(operation)
        return [item for page in paginator.paginate(**kwargs) for item in page.get(result_key, [])]

    return safe(label, collect, default)


def image_tags(image: dict) -> dict[str, str]:
    """Return an AMI's tags as a key-value mapping."""
    return {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}


def is_supported_qemu_host_image(region: str, image: dict) -> bool:
    """Return whether an AMI is eligible for automatic retention cleanup."""
    contract = CI_REGIONS.get(region)
    if contract is None:
        return False
    tags = image_tags(image)
    if any(
        tags.get(key) != value for key, value in {"role": "ci-ami", "machine": "qemu_host", "ubuntu": "noble"}.items()
    ):
        return False
    return any(
        tags.get("Name") == ami["name"]
        and (
            tags.get("architecture") in (None, "x86_64") if arch == "x86_64" else tags.get("architecture") == "aarch64"
        )
        for arch, ami in contract["amis"].items()
    )


def promoted_qemu_host_amis(region: str, images: list[dict]) -> set[str] | None:
    """Return qemu-host AMIs protected by their SSM promotion pointers."""
    contract = CI_REGIONS.get(region)
    if contract is None:
        return set()

    ubuntus = {image_tags(image)["ubuntu"] for image in images}
    promoted: set[str] = set()
    ssm = client("ssm", region)
    for ubuntu in ubuntus:
        for arch, ami in contract["amis"].items():
            parameter = ami["parameter"].format(ubuntu=ubuntu)
            try:
                value = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
            except ClientError as error:
                code = error.response["Error"].get("Code", "Error")
                if code == "ParameterNotFound":
                    continue
                errors.append(f"{region} promoted qemu-host {arch} {ubuntu}: {code}")
                return None
            except BotoCoreError as error:
                errors.append(f"{region} promoted qemu-host {arch} {ubuntu}: {type(error).__name__}: {error}")
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


def audit_asg_documents(region: str, groups: list[dict]) -> None:
    """Validate AWS pool ceilings against the runner capacity contract."""
    anomaly_count = len(anomalies)
    maxima = CI_REGIONS[region]["asg_maxima"]
    by_name = {group["AutoScalingGroupName"]: group for group in groups}
    for name, maximum in maxima.items():
        group = by_name.get(name)
        if group is None:
            anomalies.append(f"[{region}] missing Auto Scaling group {name}")
            continue
        if group.get("MinSize") != 0 or group.get("MaxSize") != maximum:
            anomalies.append(
                f"[{region}] ASG {name} bounds are {group.get('MinSize')}/{group.get('MaxSize')}, "
                f"expected 0/{maximum} from the runner capacity contract"
            )
        distribution = group.get("MixedInstancesPolicy", {}).get("InstancesDistribution", {})
        if (
            distribution.get("OnDemandBaseCapacity") != 0
            or distribution.get("OnDemandPercentageAboveBaseCapacity") != 0
        ):
            anomalies.append(f"[{region}] ASG {name} permits On-Demand capacity")
        if distribution.get("SpotAllocationStrategy") != "price-capacity-optimized":
            anomalies.append(f"[{region}] ASG {name} does not use price-capacity-optimized Spot allocation")
        if not group.get("NewInstancesProtectedFromScaleIn"):
            anomalies.append(f"[{region}] ASG {name} does not protect new instances from scale-in")
        if name == "homelab-ci-qemu-arm":
            overrides = group.get("MixedInstancesPolicy", {}).get("LaunchTemplate", {}).get("Overrides", [])
            actual_types = {
                override["InstanceType"] for override in overrides if isinstance(override.get("InstanceType"), str)
            }
            if actual_types != ARM_INSTANCE_TYPES:
                anomalies.append(
                    f"[{region}] ASG {name} instance overrides differ: "
                    f"expected {sorted(ARM_INSTANCE_TYPES)!r}, got {sorted(actual_types)!r}"
                )
            if set(group.get("AvailabilityZones", [])) != ARM_AVAILABILITY_ZONES:
                anomalies.append(f"[{region}] ASG {name} does not span all configured Frankfurt AZs")
    unexpected = sorted(set(by_name) - set(maxima))
    anomalies.extend(
        f"[{region}] unexpected CI Auto Scaling group {name}" for name in unexpected if name.startswith("homelab-ci-")
    )
    if len(anomalies) == anomaly_count:
        expected.append(f"[{region}] {len(maxima)} qemu ASGs match runner maxima")


def audit_arm_capacity_documents(quota: dict, offerings: list[dict], instance_types: list[dict]) -> None:
    """Validate the Frankfurt quota and every configured ARM metal override."""
    anomaly_count = len(anomalies)
    quota_value = quota.get("Value")
    if not isinstance(quota_value, int | float) or quota_value < ARM_REQUIRED_SPOT_VCPUS:
        anomalies.append(
            f"[eu-central-1] Standard Spot quota is {quota_value!r} vCPUs, expected at least {ARM_REQUIRED_SPOT_VCPUS}"
        )

    offered: dict[str, set[str]] = {instance_type: set() for instance_type in ARM_INSTANCE_TYPES}
    for offering in offerings:
        instance_type = offering.get("InstanceType")
        location = offering.get("Location")
        if instance_type in offered and isinstance(location, str):
            offered[instance_type].add(location)
    for instance_type, locations in offered.items():
        if not locations & ARM_AVAILABILITY_ZONES:
            anomalies.append(f"[eu-central-1] {instance_type} is unavailable in the configured Frankfurt AZs")

    specs = {spec["InstanceType"]: spec for spec in instance_types}
    for instance_type in sorted(ARM_INSTANCE_TYPES):
        spec = specs.get(instance_type)
        if spec is None:
            anomalies.append(f"[eu-central-1] missing instance-type description for {instance_type}")
            continue
        vcpus = spec.get("VCpuInfo", {}).get("DefaultVCpus", 0)
        memory = spec.get("MemoryInfo", {}).get("SizeInMiB", 0)
        storage = spec.get("InstanceStorageInfo", {})
        disk_count = sum(disk.get("Count", 0) for disk in storage.get("Disks", []))
        if vcpus < 64 or memory < 128 * 1024 or not spec.get("InstanceStorageSupported"):
            anomalies.append(f"[eu-central-1] {instance_type} is smaller than 64 vCPUs/128 GiB with instance storage")
        if disk_count < 2 or storage.get("TotalSizeInGB", 0) < 3600:
            anomalies.append(f"[eu-central-1] {instance_type} lacks two suitable local NVMe devices")
    if len(anomalies) == anomaly_count:
        expected.append("[eu-central-1] ARM Spot quota, offerings, and metal sizing validated")


def audit_promoted_image_document(region: str, image_id: str, images: list[dict], architecture: str) -> None:
    """Validate the architecture of the AMI selected by the regional pointer."""
    image = next((candidate for candidate in images if candidate.get("ImageId") == image_id), None)
    if image is None:
        anomalies.append(f"[{region}] promoted qemu-host AMI {image_id} is not readable")
    elif image.get("Architecture") != architecture:
        anomalies.append(
            f"[{region}] promoted qemu-host AMI {image_id} architecture is {image.get('Architecture')!r}, "
            f"expected {architecture!r}"
        )
    else:
        expected.append(f"[{region}] promoted {architecture} qemu-host AMI {image_id}")


def audit_guard_documents(region: str, image_block: dict, snapshot_block: dict, metadata: dict) -> None:
    """Validate the three account-level EC2 guardrails in one region."""
    anomaly_count = len(anomalies)
    if image_block.get("ImageBlockPublicAccessState") != "block-new-sharing":
        anomalies.append(f"[{region}] AMI public-access block is not enabled")
    if snapshot_block.get("State") != "block-all-sharing":
        anomalies.append(f"[{region}] snapshot public-access block is not enabled")
    account = metadata.get("AccountLevel", {})
    if account.get("HttpTokens") != "required" or account.get("HttpPutResponseHopLimit") != 1:
        anomalies.append(f"[{region}] account metadata defaults do not require IMDSv2 with hop limit 1")
    if len(anomalies) == anomaly_count:
        expected.append(f"[{region}] EC2 public-access and metadata guards checked")


def audit_ecr_documents(region: str, rules: list[dict], repositories: list[dict]) -> None:
    """Validate pull-through rules and reject repositories outside their prefixes."""
    anomaly_count = len(anomalies)
    actual = {rule.get("ecrRepositoryPrefix"): rule.get("upstreamRegistryUrl") for rule in rules}
    if actual != ECR_UPSTREAMS:
        anomalies.append(f"[{region}] ECR pull-through rules differ: expected {ECR_UPSTREAMS!r}, got {actual!r}")
    for rule in rules:
        prefix = rule.get("ecrRepositoryPrefix")
        credential_arn = rule.get("credentialArn")
        if prefix in {"docker-hub", "github", "gitlab"} and not (
            isinstance(credential_arn, str) and credential_arn.startswith(f"arn:aws:secretsmanager:{region}:")
        ):
            anomalies.append(f"[{region}] ECR rule {prefix!r} lacks a regional Secrets Manager credential")
    prefixes = tuple(f"{prefix}/" for prefix in ECR_UPSTREAMS)
    for repository in repositories:
        name = repository.get("repositoryName", "")
        if not name.startswith(prefixes):
            anomalies.append(f"[{region}] unexpected ECR repository {name!r}")
    if len(anomalies) == anomaly_count:
        expected.append(f"[{region}] {len(rules)} ECR pull-through rules checked")


def audit_bucket_documents(
    region: str,
    bucket: str,
    location: dict,
    public_access: dict,
    versioning: dict,
    policy: dict,
) -> None:
    """Validate the regional image bucket's location and access controls."""
    anomaly_count = len(anomalies)
    if location.get("LocationConstraint") != region:
        anomalies.append(f"[{region}] bucket {bucket} is in {location.get('LocationConstraint')!r}")
    access = public_access.get("PublicAccessBlockConfiguration", {})
    required_access = {"BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"}
    if not all(access.get(key) is True for key in required_access):
        anomalies.append(f"[{region}] bucket {bucket} public-access block is incomplete")
    if versioning.get("Status") != "Enabled":
        anomalies.append(f"[{region}] bucket {bucket} versioning is not enabled")
    statements = {statement.get("Sid"): statement for statement in policy.get("Statement", [])}
    insecure_transport = statements.get("DenyInsecureTransport", {})
    if insecure_transport.get("Effect") != "Deny" or insecure_transport.get("Condition") != {
        "Bool": {"aws:SecureTransport": "false"}
    }:
        anomalies.append(f"[{region}] bucket {bucket} does not deny insecure transport")
    cross_account = statements.get("DenyCrossAccountAccess", {})
    if cross_account.get("Effect") != "Deny" or cross_account.get("Condition") != {
        "StringNotEquals": {"aws:PrincipalAccount": "000390721279"}
    }:
        anomalies.append(f"[{region}] bucket {bucket} does not deny cross-account access")
    if len(anomalies) == anomaly_count:
        expected.append(f"[{region}] secure versioned image bucket {bucket}")



def audit_region_contract(region: str, ec2) -> None:
    """Audit expected standing resources for one configured CI region."""
    contract = CI_REGIONS[region]

    groups = paginated(
        f"{region} autoscaling groups",
        client("autoscaling", region),
        "describe_auto_scaling_groups",
        "AutoScalingGroups",
    )
    audit_asg_documents(region, groups)

    for architecture, ami in contract["amis"].items():
        parameter = safe(
            f"{region} {architecture} qemu-host AMI parameter",
            lambda ami=ami: client("ssm", region).get_parameter(Name=ami["parameter"].format(ubuntu="noble"))[
                "Parameter"
            ]["Value"],
            default=None,
        )
        if parameter is not None:
            images = safe(
                f"{region} promoted {architecture} qemu-host AMI",
                lambda parameter=parameter: ec2.describe_images(ImageIds=[parameter]).get("Images", []),
            )
            audit_promoted_image_document(region, parameter, images, architecture)

    image_block = safe(f"{region} AMI public access", ec2.get_image_block_public_access_state, default=None)
    snapshot_block = safe(f"{region} snapshot public access", ec2.get_snapshot_block_public_access_state, default=None)
    metadata = safe(f"{region} metadata defaults", ec2.get_instance_metadata_defaults, default=None)
    if image_block is not None and snapshot_block is not None and metadata is not None:
        audit_guard_documents(region, image_block, snapshot_block, metadata)

    key_pairs = safe(
        f"{region} operator key pair",
        lambda: ec2.describe_key_pairs(KeyNames=["homelab-ci-operator"]).get("KeyPairs", []),
        default=None,
    )
    if key_pairs is not None:
        if len(key_pairs) != 1:
            anomalies.append(f"[{region}] operator key pair is missing or ambiguous")
        else:
            expected.append(f"[{region}] operator key pair homelab-ci-operator")

    ecr = client("ecr", region)
    rules = paginated(
        f"{region} ECR pull-through rules",
        ecr,
        "describe_pull_through_cache_rules",
        "pullThroughCacheRules",
    )
    repositories = paginated(f"{region} ECR repositories", ecr, "describe_repositories", "repositories")
    audit_ecr_documents(region, rules, repositories)

    s3 = client("s3", region)
    for bucket in sorted(contract["buckets"]):
        location = safe(
            f"{region} {bucket} location", lambda bucket=bucket: s3.get_bucket_location(Bucket=bucket), default=None
        )
        public_access = safe(
            f"{region} {bucket} public access",
            lambda bucket=bucket: s3.get_public_access_block(Bucket=bucket),
            default=None,
        )
        versioning = safe(
            f"{region} {bucket} versioning", lambda bucket=bucket: s3.get_bucket_versioning(Bucket=bucket), default=None
        )
        policy_body = safe(
            f"{region} {bucket} policy",
            lambda bucket=bucket: s3.get_bucket_policy(Bucket=bucket)["Policy"],
            default=None,
        )
        if None not in (location, public_access, versioning, policy_body):
            try:
                policy = json.loads(policy_body)
            except json.JSONDecodeError as error:
                errors.append(f"{region} {bucket} policy: invalid JSON: {error}")
            else:
                audit_bucket_documents(region, bucket, location, public_access, versioning, policy)

    if region == "eu-central-1":
        quota = safe(
            "eu-central-1 Standard Spot quota",
            lambda: client("service-quotas", region).get_service_quota(
                ServiceCode="ec2", QuotaCode=STANDARD_SPOT_QUOTA_CODE
            )["Quota"],
            default=None,
        )
        offerings = paginated(
            "eu-central-1 ARM metal offerings",
            ec2,
            "describe_instance_type_offerings",
            "InstanceTypeOfferings",
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": sorted(ARM_INSTANCE_TYPES)}],
        )
        instance_types = safe(
            "eu-central-1 ARM metal descriptions",
            lambda: ec2.describe_instance_types(InstanceTypes=sorted(ARM_INSTANCE_TYPES))["InstanceTypes"],
            default=None,
        )
        if quota is not None and instance_types is not None:
            audit_arm_capacity_documents(quota, offerings, instance_types)


def sweep_region(region):
    ec2 = client("ec2", region)

    # ── Compute-shaped strays (should be none -- cells are one-time spot) ──
    anomalies.extend(
        f"[{region}] EC2 instance {i['InstanceId']} ({i['InstanceType']}, {i['State']['Name']})"
        for resv in paginated(
            f"{region} instances",
            ec2,
            "describe_instances",
            "Reservations",
        )
        for i in resv.get("Instances", [])
        if i["State"]["Name"] != "terminated"
    )

    anomalies.extend(
        f"[{region}] EBS volume {v['VolumeId']} ({v['Size']}GB, {v['State']})"
        for v in paginated(f"{region} volumes", ec2, "describe_volumes", "Volumes")
    )

    for a in safe(f"{region} addresses", lambda: ec2.describe_addresses().get("Addresses", [])):
        assoc = a.get("InstanceId") or a.get("AssociationId") or "UNASSOCIATED"
        anomalies.append(f"[{region}] Elastic IP {a['PublicIp']} ({assoc})")

    anomalies.extend(
        f"[{region}] NAT gateway {n['NatGatewayId']} ({n['State']})"
        for n in paginated(
            f"{region} nat",
            ec2,
            "describe_nat_gateways",
            "NatGateways",
        )
        if n["State"] != "deleted"
    )

    endpoints = paginated(
        f"{region} vpc-endpoints",
        ec2,
        "describe_vpc_endpoints",
        "VpcEndpoints",
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
        for lb in paginated(
            f"{region} elbv2",
            client("elbv2", region),
            "describe_load_balancers",
            "LoadBalancers",
        )
    )

    anomalies.extend(
        f"[{region}] classic ELB {lb['LoadBalancerName']}"
        for lb in paginated(
            f"{region} elb-classic",
            client("elb", region),
            "describe_load_balancers",
            "LoadBalancerDescriptions",
        )
    )

    anomalies.extend(
        f"[{region}] RDS instance {db['DBInstanceIdentifier']} ({db['DBInstanceClass']})"
        for db in paginated(
            f"{region} rds",
            client("rds", region),
            "describe_db_instances",
            "DBInstances",
        )
    )

    # ── AMIs + snapshots: distinguish legitimate cell images from orphans ──
    images = paginated(
        f"{region} images",
        ec2,
        "describe_images",
        "Images",
        default=None,
        Owners=["self"],
    )
    snaps = paginated(
        f"{region} snapshots",
        ec2,
        "describe_snapshots",
        "Snapshots",
        default=None,
        OwnerIds=["self"],
    )
    audit_ami_inventory(region, images, snaps)
    if region in CI_REGIONS:
        audit_region_contract(region, ec2)


def main():
    ident = client("sts", "eu-central-1").get_caller_identity()
    print(f"== AWS CI account audit — account {ident['Account']} as {ident['Arn']} ==\n")

    regions = safe(
        "regions",
        lambda: client("ec2", "eu-central-1").describe_regions()["Regions"],
        default=None,
    )
    region_names = [region["RegionName"] for region in regions or []]
    print(f"sweeping {len(region_names)} regions for billable strays + orphaned snapshots...")
    for region in region_names:
        sweep_region(region)

    # Account-global: S3 (terraform state is in MinIO; only CI image bundles
    # live in AWS S3).
    for b in paginated(
        "s3",
        client("s3", "eu-central-1"),
        "list_buckets",
        "Buckets",
    ):
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
        if deletes:
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

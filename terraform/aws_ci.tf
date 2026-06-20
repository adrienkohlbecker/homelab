# AWS-backed CI nested-qemu fleet (notes/ci_aws_nested_qemu_cells.md): role tests run
# on a pool of spot qemu hosts (an ASG scaled by fleeting-plugin-aws on fox),
# where each cell hydrates a promoted qemu image bundle from S3 and boots it
# under nested KVM. The bake path publishes those bundles to S3 and the host
# AMI through SSM. This file owns the platform the harness does NOT: VPC,
# security groups, IAM/OIDC, the qemu-host launch template + ASG, the image
# bucket, the ECR pull-through cache, the EventBridge Scheduler execution role,
# and the budget tripwire. Changing an instance type or spot policy happens
# here, never in test/.
#
# First-apply bootstrap (AMI parameter seeding, GitLab cutover):
# notes/ci_aws_test_cells.md, "Bootstrap".

locals {
  ci_aws_region = "eu-central-1"
  # GitLab project whose OIDC tokens may assume the CI roles. The sub claim
  # is the only identity binding (IAM has no condition key for GitLab's
  # immutable project_id), so this namespace must never be released — a
  # re-registered username could recreate the project and mint valid tokens.
  ci_gitlab_project         = "akohlbecker/homelab"
  ci_account_id             = data.aws_caller_identity.current.account_id
  ci_qemu_image_bucket_name = "homelab-ci-images"
  ci_qemu_image_machines = toset([
    "box",
    "box_deps",
  ])
  ci_qemu_host_ami_parameter = "/homelab-ci/ami/qemu-host/noble"
  ci_qemu_host_pool = {
    # Instance-type-agnostic name: aws_autoscaling_group.name and
    # aws_launch_template.name are ForceNew, so encoding the type in the name
    # would force a replacement (and churn the gitlab_runner asg_name default
    # plus the IAM groupName condition) on every future instance-type change.
    # instance_type below is the source of truth.
    name = "homelab-ci-qemu-host"
    # 16 vCPU / 32 GiB / 950 GB NVMe -- half a c8id.8xlarge, at 13 cells each
    # (~2.46 GiB/cell, same density as one 8xlarge at 26). The runner scales out
    # to as many of these as the queued burst needs, so a full universe runs in a
    # single wave instead of serial waves on one big host. max_size is the
    # 256-vCPU spot-quota ceiling, floor(256 / 16) = 16 hosts; it must match
    # gitlab_runner_aws_qemu_max_instances in host_vars/fox.yml. Per-host
    # concurrency is gitlab_runner_aws_qemu_capacity_per_instance there; a future
    # resize touches only this LT-version field, not the ASG name.
    instance_type = "c8id.4xlarge"
    max_size      = 16
    # The "d" family carries a local instance-store NVMe disk. A boot service
    # (packer/aws/files/homelab_ci_prepare_scratch.sh) formats and mounts it at
    # /mnt/scratch, so all heavy CI I/O -- qcow2 overlays and the gitlab-runner
    # checkout/cache/build tree -- lands on local NVMe, not the EBS root. The
    # root therefore only holds the OS and the baked toolchain and stays small,
    # on the gp3 free baseline (3000 IOPS / 125 MiB/s).
    root_volume_size       = 40
    root_volume_iops       = 3000
    root_volume_throughput = 125
  }
  # Dedicated single-host pool for the _site_test cell -- the full-site converge
  # is the pipeline's critical path (~25m) and was spending ~70% of its run
  # contending for CPU against the role-cell burst on the shared pool. A 4 vCPU
  # c8id.xlarge gives the box guest (4 vCPU) its own host, and decouples the cost:
  # the role-cell pool drains as soon as the cells finish instead of lingering for
  # site_test's long tail. Reuses aws_launch_template.ci_qemu_host (same AMI / SG /
  # instance profile / key); only the ASG differs, overriding instance_type. Kept
  # tagged role=ci-qemu-host so the worker boot service, homelab_ci_ready, and the
  # fleet describes treat it identically to the role-cell hosts.
  ci_qemu_site_pool = {
    name          = "homelab-ci-qemu-site"
    instance_type = "c8id.xlarge"
    max_size      = 1
  }
  ci_ecr_pull_through_cache_rules = {
    docker-hub = {
      upstream_registry_url = "registry-1.docker.io"
      credential_arn        = aws_secretsmanager_secret.ci_ecr_docker_hub.arn
    }
    github = {
      upstream_registry_url = "ghcr.io"
      credential_arn        = aws_secretsmanager_secret.ci_ecr_github.arn
    }
    gitlab = {
      upstream_registry_url = "registry.gitlab.com"
      credential_arn        = aws_secretsmanager_secret.ci_ecr_gitlab.arn
    }
    quay = {
      upstream_registry_url = "quay.io"
      credential_arn        = null
    }
  }
}

data "aws_caller_identity" "current" {}

variable "ci_ecr_docker_hub_username" {
  type        = string
  nullable    = false
  description = "Docker Hub username for ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_docker_hub_username from 1Password through `op run`."

  validation {
    condition     = length(var.ci_ecr_docker_hub_username) > 0
    error_message = "ci_ecr_docker_hub_username must be non-empty (resolved via TF_VAR_ci_ecr_docker_hub_username from 1Password through `op run`)."
  }
}

variable "ci_ecr_docker_hub_access_token" {
  type        = string
  nullable    = false
  sensitive   = true
  description = "Docker Hub access token for ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_docker_hub_access_token from 1Password through `op run`; stored in Terraform state as part of the Secrets Manager secret version."

  validation {
    condition     = length(var.ci_ecr_docker_hub_access_token) > 0
    error_message = "ci_ecr_docker_hub_access_token must be non-empty (resolved via TF_VAR_ci_ecr_docker_hub_access_token from 1Password through `op run`)."
  }
}

variable "ci_ecr_github_username" {
  type        = string
  nullable    = false
  description = "GitHub username for GHCR ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_github_username from 1Password through `op run`."

  validation {
    condition     = length(var.ci_ecr_github_username) > 0
    error_message = "ci_ecr_github_username must be non-empty (resolved via TF_VAR_ci_ecr_github_username from 1Password through `op run`)."
  }
}

variable "ci_ecr_github_access_token" {
  type        = string
  nullable    = false
  sensitive   = true
  description = "GitHub PAT for GHCR ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_github_access_token from 1Password through `op run`; stored in Terraform state as part of the Secrets Manager secret version."

  validation {
    condition     = length(var.ci_ecr_github_access_token) > 0
    error_message = "ci_ecr_github_access_token must be non-empty (resolved via TF_VAR_ci_ecr_github_access_token from 1Password through `op run`)."
  }
}

variable "ci_ecr_gitlab_username" {
  type        = string
  nullable    = false
  description = "GitLab username for registry.gitlab.com ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_gitlab_username from 1Password through `op run`."

  validation {
    condition     = length(var.ci_ecr_gitlab_username) > 0
    error_message = "ci_ecr_gitlab_username must be non-empty (resolved via TF_VAR_ci_ecr_gitlab_username from 1Password through `op run`)."
  }
}

variable "ci_ecr_gitlab_access_token" {
  type        = string
  nullable    = false
  sensitive   = true
  description = "GitLab token for registry.gitlab.com ECR pull-through cache credentials. Sourced via TF_VAR_ci_ecr_gitlab_access_token from 1Password through `op run`; stored in Terraform state as part of the Secrets Manager secret version."

  validation {
    condition     = length(var.ci_ecr_gitlab_access_token) > 0
    error_message = "ci_ecr_gitlab_access_token must be non-empty (resolved via TF_VAR_ci_ecr_gitlab_access_token from 1Password through `op run`)."
  }
}

# ─── QEMU image bundle bucket ────────────────────────────────────────────────
# Source of truth for nested-qemu cell images. Builds publish immutable
# <ubuntu>/<machine>/<build-id>/ prefixes containing manifest.json and the
# compressed disk bundle; SSM promotion points consumers at the current build.

resource "aws_s3_bucket" "ci_qemu_images" {
  bucket = local.ci_qemu_image_bucket_name

  tags = {
    Name    = local.ci_qemu_image_bucket_name
    role    = "ci"
    purpose = "qemu-images"
  }
}

resource "aws_s3_bucket_public_access_block" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "prune-old-versions"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }

    noncurrent_version_expiration {
      noncurrent_days = 14
    }
  }
}

resource "aws_s3_bucket_policy" "ci_qemu_images" {
  bucket = aws_s3_bucket.ci_qemu_images.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.ci_qemu_images.arn,
          "${aws_s3_bucket.ci_qemu_images.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        Sid       = "DenyCrossAccountAccess"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.ci_qemu_images.arn,
          "${aws_s3_bucket.ci_qemu_images.arn}/*",
        ]
        Condition = {
          StringNotEquals = { "aws:PrincipalAccount" = local.ci_account_id }
        }
      },
    ]
  })
}

# ─── ECR pull-through cache ──────────────────────────────────────────────────
# AWS cells cannot reach the lab Nexus Docker proxies. These regional ECR rules
# cache the public registries that roles pull from, so the qemu hosts fetch
# layers from eu-central-1 after the first import. Docker Hub, GHCR, and GitLab require
# upstream credentials in Secrets Manager. The secret versions are terraform-
# managed here by explicit operator choice, so the token values are present in
# the encrypted terraform state.

resource "aws_secretsmanager_secret" "ci_ecr_docker_hub" {
  name        = "ecr-pullthroughcache/docker-hub"
  description = "Docker Hub credentials for ECR pull-through cache"

  tags = {
    role = "ci"
  }
}

resource "aws_secretsmanager_secret_version" "ci_ecr_docker_hub" {
  secret_id = aws_secretsmanager_secret.ci_ecr_docker_hub.id
  secret_string = jsonencode({
    username    = var.ci_ecr_docker_hub_username
    accessToken = var.ci_ecr_docker_hub_access_token
  })
}

resource "aws_secretsmanager_secret" "ci_ecr_github" {
  name        = "ecr-pullthroughcache/github"
  description = "GHCR credentials for ECR pull-through cache"

  tags = {
    role = "ci"
  }
}

resource "aws_secretsmanager_secret_version" "ci_ecr_github" {
  secret_id = aws_secretsmanager_secret.ci_ecr_github.id
  secret_string = jsonencode({
    username    = var.ci_ecr_github_username
    accessToken = var.ci_ecr_github_access_token
  })
}

resource "aws_secretsmanager_secret" "ci_ecr_gitlab" {
  name        = "ecr-pullthroughcache/gitlab"
  description = "GitLab Container Registry credentials for ECR pull-through cache"

  tags = {
    role = "ci"
  }
}

resource "aws_secretsmanager_secret_version" "ci_ecr_gitlab" {
  secret_id = aws_secretsmanager_secret.ci_ecr_gitlab.id
  secret_string = jsonencode({
    username    = var.ci_ecr_gitlab_username
    accessToken = var.ci_ecr_gitlab_access_token
  })
}

resource "aws_ecr_pull_through_cache_rule" "ci" {
  for_each = local.ci_ecr_pull_through_cache_rules

  ecr_repository_prefix = each.key
  upstream_registry_url = each.value.upstream_registry_url
  credential_arn        = each.value.credential_arn

  depends_on = [
    aws_secretsmanager_secret_version.ci_ecr_docker_hub,
    aws_secretsmanager_secret_version.ci_ecr_github,
    aws_secretsmanager_secret_version.ci_ecr_gitlab,
  ]
}

# ─── Networking ──────────────────────────────────────────────────────────────
# Dedicated CI VPC: public IPv4 subnets across 3 AZs, IGW only — no NAT
# gateway (a ~$32/mo standing trap). Each qemu host gets an ephemeral public
# IPv4 ($0.005/hr while running). IPv6 — including IPv6-only — was investigated
# and rejected: github.com and objects.githubusercontent.com have no AAAA
# and the toolchain pulls from GitHub releases.

resource "aws_vpc" "ci" {
  cidr_block = "10.99.0.0/16"

  tags = {
    Name = "homelab-ci"
    role = "ci"
  }
}

resource "aws_subnet" "ci" {
  # The value is the IPv4 third octet.
  for_each = {
    a = 0
    b = 1
    c = 2
  }

  vpc_id            = aws_vpc.ci.id
  availability_zone = "${local.ci_aws_region}${each.key}"
  cidr_block        = cidrsubnet(aws_vpc.ci.cidr_block, 8, each.value)

  # Public IPv4 rides the subnet default rather than a launch-template
  # network_interfaces block: the harness overrides the subnet per launch
  # with a top-level --subnet-id, which EC2 rejects when the template
  # carries an explicit network-interface spec.
  map_public_ip_on_launch = true

  tags = {
    Name = "homelab-ci-${each.key}"
    role = "ci"
  }
}

resource "aws_internet_gateway" "ci" {
  vpc_id = aws_vpc.ci.id

  tags = {
    Name = "homelab-ci"
    role = "ci"
  }
}

# The default route rides the VPC's main route table: every subnet in this
# single-purpose VPC falls back to it, so a dedicated table plus per-subnet
# associations would only restate that fallback.
resource "aws_default_route_table" "ci" {
  default_route_table_id = aws_vpc.ci.default_route_table_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ci.id
  }

  tags = {
    Name = "homelab-ci"
    role = "ci"
  }
}

# ─── Security group ──────────────────────────────────────────────────────────
# Instance-executor qemu hosts, and the local AMI bakes that reuse this SG. The
# manager runs on fox outside the VPC, so fleeting must SSH to the worker's
# public address; `mise run packer:qemu-host-ami` runs packer on the operator's
# workstation, which filters this same SG, so the home WAN needs SSH in too. No
# standing Elastic IP is allocated, and public IPv4 bills only while an instance
# exists.
resource "aws_security_group" "ci_qemu_host" {
  name = "homelab-ci-qemu-host"
  # Description is immutable in AWS (ForceNew); the two ingress rules below carry
  # the real intent (fox for fleeting, home WAN for local bakes), so it stays as
  # first created rather than churning a replacement to re-word it.
  description = "CI qemu hosts: SSH from fox, open egress"
  vpc_id      = aws_vpc.ci.id

  ingress {
    description = "SSH from fox"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${hcloud_primary_ip.fox.ip_address}/32"]
  }

  # Local `packer:qemu-host-ami` / `fox_image` bakes SSH from the operator's
  # workstation, which egresses via the home WAN (residential, so it can drift
  # -- update TF_VAR_home_wan_ip and re-apply, same value the fox wg0 ingress
  # already tracks).
  ingress {
    description = "SSH from operator workstation (local bakes)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${local.home_wan_ip}/32"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "homelab-ci-qemu-host"
    role = "ci"
  }
}

# Adopt the VPC's auto-created default security group and strip its stock
# allow-all rules: nothing launches with it (the qemu host pins
# ci_qemu_host via its launch template), so codify that as zero rules.
resource "aws_default_security_group" "ci" {
  vpc_id = aws_vpc.ci.id

  tags = {
    Name = "homelab-ci-default-empty"
    role = "ci"
  }
}

# ─── GitLab OIDC ─────────────────────────────────────────────────────────────
# CI jobs request `id_tokens` with `aud: sts.amazonaws.com`, set AWS_ROLE_ARN
# + AWS_WEB_IDENTITY_TOKEN_FILE, and AssumeRoleWithWebIdentity into one of the
# two roles below. No static AWS keys anywhere in CI.

# No thumbprint_list: for issuers served behind publicly trusted CAs
# (gitlab.com qualifies) AWS validates against its own root-CA library and
# ignores thumbprints, so pinning one would only add a live TLS fetch at
# plan time and spurious diffs on certificate rotation.
resource "aws_iam_openid_connect_provider" "gitlab" {
  url            = "https://gitlab.com"
  client_id_list = ["sts.amazonaws.com"]

  tags = {
    Name = "gitlab.com"
    role = "ci"
  }
}

# ─── EventBridge Scheduler execution role ────────────────────────────────────
# Target role for the packer bake one-time termination backstop: immediately
# after a successful launch the bake wrappers (per build instance,
# mise-tasks/packer/_bake_backstop.sh) create a one-time schedule invoking EC2
# TerminateInstances through this role; normal cleanup deletes the schedule
# before it fires. The backstop matters because a CI job-timeout SIGKILL
# bypasses the wrapper's own on-error cleanup. Scoped so it can only ever
# terminate ci-ami (bake) instances. Assumed only by the kept ci_bake role.

resource "aws_iam_role" "ci_cell_scheduler" {
  name = "homelab-ci-cell-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        # Confused-deputy guard. aws:SourceArn must be a schedule GROUP —
        # scoping it to a schedule or schedule-name prefix is explicitly
        # unsupported (docs: "Confused deputy prevention in EventBridge
        # Scheduler") and fails every CreateSchedule with "execution role
        # must allow ... to assume the role". Per-instance scoping isn't lost:
        # the permission policy below only terminates role=ci-ami instances
        # regardless of which schedule fires it.
        StringEquals = {
          "aws:SourceAccount" = local.ci_account_id
          "aws:SourceArn"     = "arn:aws:scheduler:${local.ci_aws_region}:${local.ci_account_id}:schedule-group/default"
        }
      }
    }]
  })

  tags = { role = "ci" }
}

resource "aws_iam_role_policy" "ci_cell_scheduler" {
  name = "terminate-ci-instances"
  role = aws_iam_role.ci_cell_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ec2:TerminateInstances"
      Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*"
      Condition = {
        StringEquals = {
          # Bake build instances (_bake_backstop.sh) — nothing else this role
          # can ever reap.
          "ec2:ResourceTag/role" = ["ci-ami"]
        }
      }
    }]
  })
}

# ─── Cell role ───────────────────────────────────────────────────────────────
# Assumed by qemu test-cell jobs over GitLab OIDC (any branch in the project —
# branch pipelines run role tests too; tag pipelines don't, so they get
# nothing). Read-only by design: qemu cells run the harness locally on the
# shell runner and assume this role solely to hydrate the promoted qemu base
# images from S3 (mise run ci:hydrate-qemu-images — an SSM get-parameter plus an
# s3 cp). It grants nothing that launches instances, writes images, or promotes
# artifacts; the same minimal read set as the ci_qemu_host instance role.

resource "aws_iam_role" "ci_cell" {
  name = "homelab-ci-cell"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.gitlab.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "gitlab.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "gitlab.com:sub" = "project_path:${local.ci_gitlab_project}:ref_type:branch:ref:*"
        }
      }
    }]
  })

  tags = { role = "ci" }
}

resource "aws_iam_role_policy" "ci_cell" {
  name = "ci-cell-operations"
  role = aws_iam_role.ci_cell.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ResolveQemuImage"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/qemu-image/*"
      },
      {
        Sid      = "LocateQemuImages"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = aws_s3_bucket.ci_qemu_images.arn
      },
      {
        Sid      = "ReadQemuImages"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.ci_qemu_images.arn}/*"
      },
      # roles/test mirrors.yml mints an ECR login token on the cell (the
      # controller) and the qemu guest then pulls container layers through the
      # pull-through cache as this same identity. GetAuthorizationToken is only
      # valid against "*"; the repository-scoped actions cover the lazy
      # first-pull (CreateRepository + BatchImportUpstreamImage populate the
      # cache repo) and the subsequent layer fetch.
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPullThroughCache"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchImportUpstreamImage",
          "ecr:CreateRepository",
        ]
        Resource = "arn:aws:ecr:${local.ci_aws_region}:${local.ci_account_id}:repository/*"
      },
    ]
  })
}

# ─── Nested-qemu runner hosts ────────────────────────────────────────────────
# The instance-executor worker identity is deliberately harmless when exposed
# to shell jobs through IMDS: it can read promoted qemu image pointers/bundles
# and nothing that scales ASGs, writes images, or promotes artifacts.

resource "aws_iam_role" "ci_qemu_host" {
  name = "homelab-ci-qemu-host"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { role = "ci" }
}

resource "aws_iam_instance_profile" "ci_qemu_host" {
  name = "homelab-ci-qemu-host"
  role = aws_iam_role.ci_qemu_host.name

  tags = { role = "ci" }
}

resource "aws_iam_role_policy" "ci_qemu_host" {
  name = "ci-qemu-host-readonly"
  role = aws_iam_role.ci_qemu_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ResolveQemuImage"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/qemu-image/*"
      },
      {
        Sid      = "LocateQemuImages"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = aws_s3_bucket.ci_qemu_images.arn
      },
      {
        Sid      = "ReadQemuImages"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.ci_qemu_images.arn}/*"
      },
    ]
  })
}

# Manager-side AWS identity for fleeting-plugin-aws on fox. Terraform owns the
# least-privilege policy and user; the access key itself is minted separately
# and vaulted into gitlab_runner_aws_qemu_* so the secret does not have to move
# through Terraform outputs.
resource "aws_iam_user" "ci_fleeting_manager" {
  name = "homelab-ci-fleeting"
  path = "/"

  tags = { role = "ci" }
}

resource "aws_iam_user_policy" "ci_fleeting_manager" {
  name = "ci-fleeting-manager"
  user = aws_iam_user.ci_fleeting_manager.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ScaleQemuHostAsg"
        Effect = "Allow"
        Action = [
          "autoscaling:SetDesiredCapacity",
          "autoscaling:SetInstanceProtection",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
        ]
        Resource = [
          aws_autoscaling_group.ci_qemu_host.arn,
          aws_autoscaling_group.ci_qemu_site.arn,
        ]
      },
      {
        Sid    = "DescribeQemuHostFleet"
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeScalingActivities",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeSpotInstanceRequests",
        ]
        Resource = "*"
      },
      {
        Sid      = "ConnectToQemuHost"
        Effect   = "Allow"
        Action   = "ec2-instance-connect:SendSSHPublicKey"
        Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/aws:autoscaling:groupName" = [
              local.ci_qemu_host_pool.name,
              local.ci_qemu_site_pool.name,
            ]
          }
        }
      },
    ]
  })
}

# ─── Bake role ───────────────────────────────────────────────────────────────
# Assumed only by protected-ref jobs (master): packer ebssurrogate bakes,
# AMI registration, and SSM promotion. Branch jobs cannot publish AMIs.
# Broader than the cell role by necessity (packer creates temp keypairs,
# volumes, snapshots, and registers images) but pinned to the CI region.

resource "aws_iam_role" "ci_bake" {
  name = "homelab-ci-bake"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.gitlab.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "gitlab.com:aud" = "sts.amazonaws.com"
          "gitlab.com:sub" = "project_path:${local.ci_gitlab_project}:ref_type:branch:ref:master"
        }
      }
    }]
  })

  tags = { role = "ci" }
}

resource "aws_iam_role_policy" "ci_bake" {
  name = "ci-bake-operations"
  role = aws_iam_role.ci_bake.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PackerBake"
        Effect = "Allow"
        Action = [
          # Build instance lifecycle. ModifyInstanceAttribute is packer's
          # ENA-enable step (ena_support = true in packer/aws/ami.pkr.hcl,
          # load-bearing: Nitro instance types refuse non-ENA AMIs).
          "ec2:RunInstances", "ec2:StopInstances", "ec2:TerminateInstances",
          "ec2:DescribeInstances", "ec2:DescribeInstanceStatus",
          "ec2:GetConsoleOutput", "ec2:ModifyInstanceAttribute",
          # ebssurrogate volumes and the snapshots behind the AMI.
          "ec2:CreateVolume", "ec2:DeleteVolume", "ec2:AttachVolume",
          "ec2:DetachVolume", "ec2:DescribeVolumes",
          "ec2:CreateSnapshot", "ec2:DeleteSnapshot", "ec2:DescribeSnapshots",
          "ec2:ModifySnapshotAttribute",
          # AMI registration + retention pruning.
          "ec2:RegisterImage", "ec2:DeregisterImage", "ec2:CreateImage",
          "ec2:DescribeImages", "ec2:DescribeImageAttribute",
          "ec2:ModifyImageAttribute",
          # Packer's ephemeral keypair + tagging.
          "ec2:CreateKeyPair", "ec2:DeleteKeyPair", "ec2:DescribeKeyPairs",
          "ec2:CreateTags", "ec2:DescribeTags",
          # Discovery.
          "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
          "ec2:DescribeAvailabilityZones", "ec2:DescribeRegions",
          "ec2:DescribeVpcs", "ec2:DescribeLaunchTemplates",
          "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeInstanceTypes",
          "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSpotPriceHistory",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = local.ci_aws_region }
        }
      },
      # Candidate write + current promotion of the nested-qemu host AMI
      # parameter (the only AMI a bake still promotes — the qemu base images
      # themselves ride S3, see PromoteQemuImages below).
      {
        Sid    = "PromoteAmi"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters",
          "ssm:DescribeParameters", "ssm:AddTagsToResource",
        ]
        Resource = [
          "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter${local.ci_qemu_host_ami_parameter}",
        ]
      },
      {
        Sid    = "PublishQemuImages"
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation", "s3:ListBucket",
          "s3:ListBucketMultipartUploads", "s3:ListBucketVersions",
        ]
        Resource = aws_s3_bucket.ci_qemu_images.arn
      },
      {
        Sid    = "WriteQemuImages"
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject",
          "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
          "s3:DeleteObject", "s3:DeleteObjectVersion",
        ]
        Resource = "${aws_s3_bucket.ci_qemu_images.arn}/*"
      },
      {
        Sid    = "PromoteQemuImages"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters",
          "ssm:DescribeParameters", "ssm:AddTagsToResource",
        ]
        Resource = [
          for m in local.ci_qemu_image_machines :
          "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/qemu-image/${m}/*"
        ]
      },
      # Per-bake one-time termination schedules (mise-tasks/packer/
      # _bake_backstop.sh): a CI job-timeout SIGKILL bypasses the wrapper's
      # on-error cleanup, so the schedule is the only thing that reaps an
      # orphaned build instance.
      {
        Sid      = "BakeSchedules"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"]
        Resource = "arn:aws:scheduler:${local.ci_aws_region}:${local.ci_account_id}:schedule/default/ci-bake-*"
      },
      {
        Sid      = "PassSchedulerRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.ci_cell_scheduler.arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" }
        }
      },
    ]
  })
}

# ─── Account guards ──────────────────────────────────────────────────────────
# Regional one-liners that bound what any principal — including a compromised
# CI role — can do, at zero standing cost.

# Nothing in this account should ever be shared publicly; the bake role holds
# ModifyImageAttribute/ModifySnapshotAttribute on *, so block the
# public-sharing path account-wide.
resource "aws_ec2_image_block_public_access" "ci" {
  state = "block-new-sharing"
}

resource "aws_ebs_snapshot_block_public_access" "ci" {
  state = "block-all-sharing"
}

# IMDSv2 as the regional floor for every launch path (console debugging,
# future templates), not just the qemu-host template that already requires it.
resource "aws_ec2_instance_metadata_defaults" "ci" {
  http_tokens                 = "required"
  http_put_response_hop_limit = 1
}

# Spot launches need the EC2 Spot service-linked role to exist: the qemu-host
# ASG runs spot (mixed_instances_policy), and the fleeting manager (rightly)
# cannot create service-linked roles — without this, the first spot launch in a
# fresh account fails with an opaque AuthFailure. If the account already grew it
# out of band, `tofu import` it instead of applying.
resource "aws_iam_service_linked_role" "spot" {
  aws_service_name = "spot.amazonaws.com"
}

# ─── Operator SSH key ────────────────────────────────────────────────────────
# The operator's personal public key (group_vars/all/main.yml
# ssh_public_keys), registered as an EC2 keypair so the nested-qemu host
# launch template authorizes it on every ASG instance: the fleeting manager on
# fox SSHes in over the public IP, a real boundary alongside ci_qemu_host's SG.
# The private half lives only in the operator's ssh agent.

resource "aws_key_pair" "ci_operator" {
  key_name   = "homelab-ci-operator"
  public_key = local.operator_ssh_public_key

  tags = { role = "ci" }
}

# ─── Launch template: nested-qemu host pool ──────────────────────────────────
# GitLab Runner/fleeting owns desired capacity, and the host runs shell jobs
# that launch qemu/KVM locally. The AMI is resolved through SSM
# (ci_qemu_host_ami_parameter), so a host bake promotion never touches
# terraform.
resource "aws_launch_template" "ci_qemu_host" {
  name                   = local.ci_qemu_host_pool.name
  description            = "homelab CI nested-qemu host"
  update_default_version = true

  image_id      = "resolve:ssm:${local.ci_qemu_host_ami_parameter}"
  instance_type = local.ci_qemu_host_pool.instance_type

  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    arn = aws_iam_instance_profile.ci_qemu_host.arn
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  cpu_options {
    nested_virtualization = "enabled"
  }

  block_device_mappings {
    device_name = "/dev/sda1"

    ebs {
      volume_size           = local.ci_qemu_host_pool.root_volume_size
      volume_type           = "gp3"
      iops                  = local.ci_qemu_host_pool.root_volume_iops
      throughput            = local.ci_qemu_host_pool.root_volume_throughput
      encrypted             = true
      delete_on_termination = true
    }
  }

  # Public IPv4 comes from the subnet default. The manager is outside the VPC,
  # so no network_interfaces block here: keep the SG/subnet on top-level fields
  # and let the ASG choose an AZ.
  vpc_security_group_ids = [aws_security_group.ci_qemu_host.id]
  key_name               = aws_key_pair.ci_operator.key_name

  tag_specifications {
    resource_type = "instance"
    tags = {
      role = "ci-qemu-host"
      pool = local.ci_qemu_host_pool.name
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      role = "ci-qemu-host"
      pool = local.ci_qemu_host_pool.name
    }
  }

  tags = {
    Name = local.ci_qemu_host_pool.name
    role = "ci"
    pool = local.ci_qemu_host_pool.name
  }
}

resource "aws_autoscaling_group" "ci_qemu_host" {
  name                  = local.ci_qemu_host_pool.name
  min_size              = 0
  max_size              = local.ci_qemu_host_pool.max_size
  desired_capacity      = 0
  protect_from_scale_in = true
  health_check_type     = "EC2"
  suspended_processes   = ["AZRebalance"]
  vpc_zone_identifier   = [for subnet in aws_subnet.ci : subnet.id]

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.ci_qemu_host.id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Name"
    value               = local.ci_qemu_host_pool.name
    propagate_at_launch = true
  }

  tag {
    key                 = "role"
    value               = "ci-qemu-host"
    propagate_at_launch = true
  }

  tag {
    key                 = "pool"
    value               = local.ci_qemu_host_pool.name
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

# Dedicated _site_test pool (see local.ci_qemu_site_pool). Reuses the role-cell
# launch template -- same AMI, SG, instance profile, key, NVMe boot service --
# and only overrides the instance type to the 4 vCPU c8id.xlarge. max_size = 1:
# the site runner block pins capacity_per_instance = max_instances = 1, so this
# ASG never holds more than the single site_test host.
resource "aws_autoscaling_group" "ci_qemu_site" {
  name                  = local.ci_qemu_site_pool.name
  min_size              = 0
  max_size              = local.ci_qemu_site_pool.max_size
  desired_capacity      = 0
  protect_from_scale_in = true
  health_check_type     = "EC2"
  suspended_processes   = ["AZRebalance"]
  vpc_zone_identifier   = [for subnet in aws_subnet.ci : subnet.id]

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.ci_qemu_host.id
        version            = "$Latest"
      }

      # Only divergence from the role-cell pool: a smaller host for the lone
      # site_test guest. The launch template's own instance_type is the
      # role-cell default; this override wins for this ASG.
      override {
        instance_type = local.ci_qemu_site_pool.instance_type
      }
    }
  }

  tag {
    key                 = "Name"
    value               = local.ci_qemu_site_pool.name
    propagate_at_launch = true
  }

  tag {
    key                 = "role"
    value               = "ci-qemu-host"
    propagate_at_launch = true
  }

  tag {
    key                 = "pool"
    value               = local.ci_qemu_site_pool.name
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

# ─── SSM parameter: nested-qemu host AMI ─────────────────────────────────────
# The nested-qemu host AMI promotion path needs a real parameter before the ASG
# launch template can reference it. Terraform owns the parameter shell, seeding
# it with Canonical's current noble AMI; packer owns the promoted AMI value
# after the first successful host bake.

data "aws_ssm_parameter" "canonical_ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_ssm_parameter" "ci_qemu_host_ami" {
  name      = local.ci_qemu_host_ami_parameter
  type      = "String"
  data_type = "aws:ec2:image"
  value     = data.aws_ssm_parameter.canonical_ubuntu.value

  tags = {
    role    = "ci-ami"
    machine = "qemu_host"
    ubuntu  = "noble"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# ─── Budget tripwire ─────────────────────────────────────────────────────────
# Account-wide (the account holds nothing but CI), $10/mo. Alert-only —
# budgets ride Cost Explorer data that lags hours, and forecast alerts need
# weeks of billing history before AWS emits them at all — so the hard spend
# ceilings are the qemu-host pool's instance-type/size and the spot quota; this
# is the operator's tripwire, not containment.

resource "aws_budgets_budget" "ci" {
  name         = "homelab-ci"
  budget_type  = "COST"
  limit_amount = "10"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["adrien.kohlbecker@gmail.com"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["adrien.kohlbecker@gmail.com"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = ["adrien.kohlbecker@gmail.com"]
  }
}

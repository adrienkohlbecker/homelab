# AWS-backed CI test cells (notes/ci_aws_test_cells.md): every role test runs
# against a single-use EC2 spot instance launched from a per-machine launch
# template, with the AMI resolved through SSM. This file owns everything the
# harness does NOT: VPC, security group, IAM/OIDC, launch templates, the
# EventBridge Scheduler execution role, and the budget tripwire. Changing an
# instance type or spot policy happens here, never in test/.
#
# First-apply bootstrap (AMI parameter seeding, GitLab cutover):
# notes/ci_aws_test_cells.md, "Bootstrap".

locals {
  ci_aws_region = "eu-central-1"
  # GitLab project whose OIDC tokens may assume the CI roles. The sub claim
  # is the only identity binding (IAM has no condition key for GitLab's
  # immutable project_id), so this namespace must never be released — a
  # re-registered username could recreate the project and mint valid tokens.
  ci_gitlab_project = "akohlbecker/homelab"
  ci_account_id     = data.aws_caller_identity.current.account_id
  wan_probe_ports   = yamldecode(file("${path.module}/../data/wan_probe_ports.yml"))
  ci_cell_wan_probe_rules = flatten([
    for protocol, ports in local.wan_probe_ports : [
      for port in ports : {
        protocol = protocol
        port     = port
      }
    ]
  ])
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

# ─── ECR pull-through cache ──────────────────────────────────────────────────
# AWS cells cannot reach the lab Nexus Docker proxies. These regional ECR rules
# cache the public registries that roles pull from, so EC2 cells fetch layers
# from eu-central-1 after the first import. Docker Hub, GHCR, and GitLab require
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
# gateway (a ~$32/mo standing trap). Each cell gets an ephemeral public IPv4
# ($0.005/hr while running). IPv6 — including IPv6-only — was investigated
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
# Ingress only from the fleet's two operators: fox (the runner host converging
# cells over SSH) and the home WAN (deliberate operator access for debugging,
# not a separate emergency profile). Cells authenticate with the baked operator
# key (aws_key_pair.ci_operator below); this group is the second boundary that
# replaces qemu's loopback hostfwd. Besides SSH, the same two sources may reach
# the firewall role's _verify WAN-probe targets from data/wan_probe_ports.yml —
# on EC2 the controller probes the cell's public IP directly, real WAN ingress
# standing in for qemu's loopback forwards (test/machine.py wan_probe_host).

resource "aws_security_group" "ci_cell" {
  name = "homelab-ci-cell"
  # Immutable in AWS (a reword forces SG replacement, cascading into every
  # launch template) — stale wording is cheaper than the churn; the header
  # comment above is the living description.
  description = "CI test cells: SSH from fox + home WAN, open egress"
  vpc_id      = aws_vpc.ci.id

  ingress {
    description = "SSH from fox + home WAN"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [
      "${hcloud_primary_ip.fox.ip_address}/32",
      "${local.home_wan_ip}/32",
    ]
  }

  dynamic "ingress" {
    for_each = {
      for rule in local.ci_cell_wan_probe_rules :
      "${rule.protocol}-${rule.port}" => rule
    }

    content {
      description = "firewall _verify WAN probe ${ingress.value.protocol}/${ingress.value.port}"
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = ingress.value.protocol
      cidr_blocks = [
        "${hcloud_primary_ip.fox.ip_address}/32",
        "${local.home_wan_ip}/32",
      ]
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "homelab-ci-cell"
    role = "ci"
  }
}

# Adopt the VPC's auto-created default security group and strip its stock
# allow-all rules: nothing launches with it (cells pin ci_cell via the
# launch template), so codify that as zero rules.
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
# Target role for the one-time termination backstops: immediately after a
# successful launch the harness (per cell) and the packer bake wrappers (per
# build instance, mise-tasks/packer/_bake_backstop.sh) create a one-time
# schedule invoking EC2 TerminateInstances through this role; normal cleanup
# deletes the schedule before it fires. The backstop matters because a CI
# job-timeout SIGKILL bypasses the wrapper's own on-error cleanup. Scoped so it
# can only ever terminate ci-cell (test cell) or ci-ami (bake) instances.

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
        # the permission policy below only terminates role=ci-cell / ci-ami
        # instances regardless of which schedule fires it.
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
          # Test cells (harness backstop) and bake build instances
          # (_bake_backstop.sh) — nothing else this role can ever reap.
          "ec2:ResourceTag/role" = ["ci-cell", "ci-ami"]
        }
      }
    }]
  })
}

# ─── Cell role ───────────────────────────────────────────────────────────────
# Assumed by test-cell jobs (any branch in the project — branch pipelines run
# role tests too; tag pipelines don't, so they get nothing). Deny-by-default
# in shape: RunInstances only through the cell launch templates into the CI
# subnets/SG with the role=ci-cell tag and the templates' instance types,
# terminate/console only for ci-cell instances, PassRole only of the
# scheduler role to EventBridge Scheduler.

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
      # RunInstances splits across statements because aws:RequestTag only
      # evaluates against resources that receive tags in the request
      # (instance + volume, via the launch template tag_specifications);
      # putting it on subnet/SG/AMI ARNs would deny every launch. The
      # instance statement also pins the instance type: launch-template
      # values are caller-overridable at run-instances time, so without it
      # a leaked token could launch metal sizes through the template.
      {
        Sid      = "RunTaggedInstance"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/role" = "ci-cell"
            "ec2:InstanceType"    = distinct(values(local.ci_machine_instance_types))
          }
          ArnEquals = {
            "ec2:LaunchTemplate" = [for lt in aws_launch_template.ci_cell : lt.arn]
          }
        }
      },
      # ec2:InstanceType is not in the volume resource's condition context,
      # so the volume statement carries only the tag + template conditions.
      {
        Sid      = "RunTaggedVolume"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:volume/*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/role" = "ci-cell"
          }
          ArnEquals = {
            "ec2:LaunchTemplate" = [for lt in aws_launch_template.ci_cell : lt.arn]
          }
        }
      },
      # AMIs sit apart from the other supporting resources to pin the owner:
      # self-baked images plus Canonical's (minimal). Without the condition
      # the token could launch any public or marketplace AMI. Canonical's
      # public Ubuntu images surface under the "amazon" owner-alias, so
      # ec2:Owner reports "amazon" rather than account 099720109477 — match
      # the alias to allow the minimal machine's Canonical AMI, while the
      # account id keeps the self-baked box/pug/lab images launchable.
      {
        Sid      = "RunImage"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:${local.ci_aws_region}::image/*"
        Condition = {
          StringEquals = {
            "ec2:Owner" = [local.ci_account_id, "amazon"]
          }
        }
      },
      {
        Sid    = "RunSupporting"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = concat(
          [for lt in aws_launch_template.ci_cell : lt.arn],
          [for s in aws_subnet.ci : s.arn],
          [
            aws_security_group.ci_cell.arn,
            aws_key_pair.ci_operator.arn,
            "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:network-interface/*",
            "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:spot-instances-request/*",
          ]
        )
      },
      # Launch-time tagging of the instance/volume (the LT tag_specifications
      # plus the harness's per-cell tags ride this).
      {
        Sid      = "TagOnLaunch"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:CreateAction" = "RunInstances" }
        }
      },
      # Lifecycle of ci-cell instances only.
      {
        Sid      = "CellLifecycle"
        Effect   = "Allow"
        Action   = ["ec2:TerminateInstances", "ec2:GetConsoleOutput"]
        Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/role" = "ci-cell" }
        }
      },
      # Describe* supports no resource-level scoping in EC2. DescribeSubnets
      # lets the harness discover the CI subnets by tag to pick one per launch.
      {
        Sid      = "Describe"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeSubnets"]
        Resource = "*"
      },
      # AMI resolution: GetParameters (plural) is what EC2 calls server-side
      # for resolve:ssm: image IDs in RunInstances. Only our namespace —
      # minimal's alias parameter (below) already carries the resolved
      # Canonical AMI id, so cells never read Canonical's public path.
      {
        Sid      = "ResolveAmi"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/ami/*"
      },
      # ECR pull-through cache for container images inside AWS cells. The
      # first pull through a namespace may create the cache repository and
      # import upstream layers; later pulls are ordinary ECR reads. Token
      # retrieval is registry-scoped by AWS and therefore resource "*".
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPullThroughCache"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:BatchImportUpstreamImage",
          "ecr:CreateRepository",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = [
          for prefix in keys(local.ci_ecr_pull_through_cache_rules) :
          "arn:aws:ecr:${local.ci_aws_region}:${local.ci_account_id}:repository/${prefix}/*"
        ]
      },
      # Per-cell one-time termination schedules.
      {
        Sid      = "CellSchedules"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"]
        Resource = "arn:aws:scheduler:${local.ci_aws_region}:${local.ci_account_id}:schedule/default/ci-cell-*"
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
      # Candidate write + current promotion of the baked machines' AMI
      # parameters. minimal's aliases are terraform-owned (ci_ami_minimal
      # below), so the bake role must not be able to clobber them.
      {
        Sid    = "PromoteAmi"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters",
          "ssm:DescribeParameters", "ssm:AddTagsToResource",
        ]
        Resource = [
          for m in keys(local.ci_machine_instance_types) :
          "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/ami/${m}/*"
          if m != "minimal"
        ]
      },
      # Per-bake one-time termination schedules (mise-tasks/packer/
      # _bake_backstop.sh), mirroring the cell role's CellSchedules: a CI
      # job-timeout SIGKILL bypasses the wrapper's on-error cleanup, so the
      # schedule is the only thing that reaps an orphaned build instance.
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
# future templates), not just the cell templates that already require it.
resource "aws_ec2_instance_metadata_defaults" "ci" {
  http_tokens                 = "required"
  http_put_response_hop_limit = 1
}

# Spot launches need the EC2 Spot service-linked role to exist, and the cell
# role (rightly) cannot create service-linked roles — without this, the first
# spot launch in a fresh account fails with an opaque AuthFailure. If the
# account already grew it out of band, `tofu import` it instead of applying.
resource "aws_iam_service_linked_role" "spot" {
  aws_service_name = "spot.amazonaws.com"
}

# ─── Cell SSH key ────────────────────────────────────────────────────────────
# The operator's personal public key (group_vars/all/main.yml
# ssh_public_keys) — NOT the well-known vagrant key the qemu fixtures bake:
# cells sit on public IPs, so unlike the loopback-hostfwd qemu path the key
# is a real boundary alongside the security group. The private half lives
# only in the operator's ssh agent; the harness passes no -i for EC2 cells.
# Registered as an EC2 keypair so cloud-init on minimal's Canonical AMI
# (which has no baked key) installs it for the ubuntu user; the baked
# machines get the same key from packer/aws/ami.pkr.hcl's SSH_KEY_PUB.

resource "aws_key_pair" "ci_operator" {
  key_name   = "homelab-ci-operator"
  public_key = local.operator_ssh_public_key

  tags = { role = "ci" }
}

# ─── Launch templates ────────────────────────────────────────────────────────
# One per machine. The AMI is deliberately NOT set here: the harness passes
# --image-id resolve:ssm:/homelab-ci/ami/<machine>/<ubuntu> per launch, so a
# bake promotion never touches terraform. Disk layout (multi-disk pug/lab)
# lives in the AMI's block device mapping, not here. Instance types settled
# by the gate-C benchmark (notes/ci_aws_test_cells.md): apt/dpkg-heavy cells
# are CPU-bound, and c6a.large (dedicated Zen3) runs them ~1.5-1.6x faster
# than t3a.medium (burstable Zen1) at near-identical per-cell cost — the
# higher rate is offset by the shorter run. minimal stays burstable: its
# cells already beat the lab baseline and never sustain CPU. Re-benchmark
# candidates with HOMELAB_EC2_INSTANCE_TYPE (test/machine.py) before
# changing these.

locals {
  ci_machine_instance_types = {
    minimal  = "t3a.small" # 2 GiB, mirrors qemu minimal
    box      = "c6a.large" # 2 vCPU / 4 GiB
    box_deps = "m6a.large" # box-class Zen3 cores with the 8 GiB floor (>4 needed, see QEMU_MACHINE_SPECS)
    pug      = "c6a.large"
    lab      = "c6a.large"
  }

  # Optional root-volume IOPS/throughput override per machine. Cells otherwise
  # inherit the AMI snapshot's gp3 defaults (3000 IOPS / 125 MiB/s). box carries
  # a maxed-out gp3 (16000 IOPS / 1000 MiB/s) to isolate whether the full-site
  # converge (_site_test:box) is EBS-IOPS bound: the small-file initramfs/dpkg
  # storm on a 4 GiB box re-reads the module tree cold once page cache is
  # evicted, and the 3000-IOPS baseline is the suspected ceiling. Maxed so the
  # volume is never the limiter -- if it helps, tune to a production value; if
  # not, IO was not the bottleneck. m6a.large (8 GiB) was tested first and ruled
  # RAM out (no win, slightly slower, worse spot). See notes/ci_aws_test_cells.md.
  ci_machine_root_volume = {
    box = { iops = 16000, throughput = 1000 }
  }
}

resource "aws_launch_template" "ci_cell" {
  for_each = local.ci_machine_instance_types

  name                   = "homelab-ci-cell-${each.key}"
  description            = "homelab CI test cell (${each.key})"
  update_default_version = true

  instance_type = each.value

  # Override the AMI root volume's gp3 IOPS/throughput for machines listed in
  # ci_machine_root_volume; others inherit the snapshot defaults (no block
  # emitted). device_name is the AMI root (/dev/sda1, see packer ami_root_device);
  # volume_size is omitted so the snapshot size and encryption carry through.
  dynamic "block_device_mappings" {
    for_each = lookup(local.ci_machine_root_volume, each.key, null) != null ? [local.ci_machine_root_volume[each.key]] : []
    content {
      device_name = "/dev/sda1"
      ebs {
        volume_type           = "gp3"
        iops                  = block_device_mappings.value.iops
        throughput            = block_device_mappings.value.throughput
        delete_on_termination = true
      }
    }
  }

  # One-time spot, terminate on interruption: a cell is never worth
  # stopping/resuming. The harness classifies interruption post-hoc and
  # exits 86 for the GitLab-level retry.
  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  instance_initiated_shutdown_behavior = "terminate"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # No iam_instance_profile: cells are converged over SSH from fox and never
  # call AWS APIs themselves.

  # Subnet (and thus AZ) is chosen by the harness at launch via a top-level
  # --subnet-id, so the template must NOT carry a network_interfaces block
  # (EC2 rejects mixing the two). Public IPv4 comes from the subnet's
  # map_public_ip_on_launch; the SG rides the top-level field.
  vpc_security_group_ids = [aws_security_group.ci_cell.id]

  # Ignored by the baked images (no cloud-init — the operator key is baked
  # by chroot.sh via SSH_KEY_PUB), consumed by minimal's Canonical AMI where
  # cloud-init installs it for the ubuntu user.
  key_name = aws_key_pair.ci_operator.key_name

  # minimal's Canonical AMI authorizes a single key via key_name (the
  # operator's personal key, for local agent-driven runs). The baked AMIs
  # additionally bake the homelab-ci-cell CI key via packer's
  # operator_ssh_keys, but minimal has no bake step — so add that second key
  # through cloud-init here, otherwise CI (whose agent holds only
  # homelab-ci-cell) cannot SSH into a minimal cell. Baked images ignore
  # user_data (no cloud-init), so scope it to minimal to avoid a no-op
  # template churn on the others.
  user_data = each.key == "minimal" ? base64encode(<<-EOT
    #cloud-config
    ssh_authorized_keys:
      - ${local.operator_ssh_public_key}
      - ${local.ci_cell_ssh_public_key}
  EOT
  ) : null

  tag_specifications {
    resource_type = "instance"
    tags = {
      role    = "ci-cell"
      machine = each.key
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      role    = "ci-cell"
      machine = each.key
    }
  }

  tags = {
    Name    = "homelab-ci-cell-${each.key}"
    role    = "ci"
    machine = each.key
  }
}

# ─── SSM parameters: minimal aliases ─────────────────────────────────────────
# minimal needs no bake: Canonical publishes per-release AMIs under public SSM
# parameters; our path just aliases theirs so the harness resolves every
# machine through the same /homelab-ci/ami/<machine>/<ubuntu> shape. A tofu
# apply refreshes the alias to Canonical's current AMI. Parameters for the
# baked machines (box/box_deps/pug/lab) are written by the packer:ami
# pipeline, not managed here.

data "aws_ssm_parameter" "canonical_ubuntu" {
  for_each = {
    # jammy predates Canonical's gp3 layout and only publishes ebs-gp2
    # (matching its hvm-ssd AMI name pattern in packer/aws/ami.pkr.hcl).
    jammy    = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
    noble    = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
    resolute = "/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
  }
  name = each.value
}

resource "aws_ssm_parameter" "ci_ami_minimal" {
  for_each = data.aws_ssm_parameter.canonical_ubuntu

  name = "/homelab-ci/ami/minimal/${each.key}"
  type = "String"
  # EC2 validates the value is a well-formed, existing AMI at write time,
  # so a bad alias fails the apply instead of the next cell launch.
  data_type = "aws:ec2:image"
  value     = each.value.value

  tags = {
    role    = "ci-ami"
    machine = "minimal"
    ubuntu  = each.key
  }
}

# ─── Budget tripwire ─────────────────────────────────────────────────────────
# Account-wide (the account holds nothing but CI), $10/mo. Alert-only —
# budgets ride Cost Explorer data that lags hours, and forecast alerts need
# weeks of billing history before AWS emits them at all — so the hard spend
# ceilings are the cell role's instance-type pin and the spot quota; this is
# the operator's tripwire, not containment.

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

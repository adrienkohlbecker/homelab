# AWS-backed CI test cells (notes/ci_aws_test_cells.md): every role test runs
# against a single-use EC2 spot instance launched from a per-machine launch
# template, with the AMI resolved through SSM. This file owns everything the
# harness does NOT: VPC, security group, IAM/OIDC, launch templates, the
# EventBridge Scheduler execution role, and the budget tripwire. Changing an
# instance type or spot policy happens here, never in test/.
#
# Bootstrap (manual, once, after apply):
#   1. Until the first packer:ami bake, seed the box AMI parameter from the
#      gate-A artifact so the harness can resolve it:
#        aws ssm put-parameter --region eu-central-1 \
#          --name /homelab-ci/ami/box/noble --type String \
#          --value ami-08f50eebedb583d9f --overwrite
#   2. At GitLab cutover, set the project CI/CD variables AWS_ROLE_ARN
#      (ci_cell_role_arn output) for cell jobs and the bake role for
#      protected ami_images jobs.

locals {
  ci_aws_region = "eu-central-1"
  # GitLab project whose OIDC tokens may assume the CI roles.
  ci_gitlab_project = "adrienkohlbecker/homelab"
  ci_account_id     = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}

# ─── Networking ──────────────────────────────────────────────────────────────
# Dedicated CI VPC: dual-stack public subnets across 3 AZs, IGW only — no NAT
# gateway (a ~$32/mo standing trap). Each cell gets an ephemeral public IPv4
# ($0.005/hr while running). IPv6-only was investigated and rejected:
# github.com and objects.githubusercontent.com have no AAAA and the toolchain
# pulls from GitHub releases.

resource "aws_vpc" "ci" {
  cidr_block                       = "10.99.0.0/16"
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  enable_dns_hostnames             = true

  tags = {
    Name = "homelab-ci"
    role = "ci"
  }
}

resource "aws_subnet" "ci" {
  # idx doubles as the IPv4 third octet and the IPv6 subnet index.
  for_each = {
    a = 0
    b = 1
    c = 2
  }

  vpc_id            = aws_vpc.ci.id
  availability_zone = "${local.ci_aws_region}${each.key}"
  cidr_block        = cidrsubnet(aws_vpc.ci.cidr_block, 8, each.value)
  ipv6_cidr_block   = cidrsubnet(aws_vpc.ci.ipv6_cidr_block, 8, each.value)

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

resource "aws_route_table" "ci" {
  vpc_id = aws_vpc.ci.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ci.id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.ci.id
  }

  tags = {
    Name = "homelab-ci"
    role = "ci"
  }
}

resource "aws_route_table_association" "ci" {
  for_each = aws_subnet.ci

  subnet_id      = each.value.id
  route_table_id = aws_route_table.ci.id
}

# ─── Security group ──────────────────────────────────────────────────────────
# 22/tcp from the fleet's two operators: fox (the runner host converging cells
# over SSH) and the home WAN (deliberate operator access for debugging, not a
# separate emergency profile). Cells authenticate with the baked vagrant key;
# this group is the trust boundary that replaces qemu's loopback hostfwd.

resource "aws_security_group" "ci_cell" {
  name        = "homelab-ci-cell"
  description = "CI test cells: SSH from fox + home WAN, open egress"
  vpc_id      = aws_vpc.ci.id

  ingress {
    description = "SSH from fox"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${hcloud_primary_ip.fox.ip_address}/32"]
  }

  ingress {
    description = "SSH from home WAN"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${local.home_wan_ip}/32"]
  }

  egress {
    description      = "All outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = {
    Name = "homelab-ci-cell"
    role = "ci"
  }
}

# ─── GitLab OIDC ─────────────────────────────────────────────────────────────
# CI jobs request `id_tokens` with `aud: sts.amazonaws.com`, set AWS_ROLE_ARN
# + AWS_WEB_IDENTITY_TOKEN_FILE, and AssumeRoleWithWebIdentity into one of the
# two roles below. No static AWS keys anywhere in CI.

data "tls_certificate" "gitlab" {
  url = "https://gitlab.com"
}

resource "aws_iam_openid_connect_provider" "gitlab" {
  url             = "https://gitlab.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.gitlab.certificates[0].sha1_fingerprint]

  tags = {
    Name = "gitlab.com"
    role = "ci"
  }
}

# ─── EventBridge Scheduler execution role ────────────────────────────────────
# Target role for the per-cell one-time termination backstop: immediately
# after a successful launch the harness creates a one-time schedule invoking
# EC2 TerminateInstances through this role; normal cleanup deletes the
# schedule before it fires. Scoped so it can only ever terminate ci-cell
# instances.

resource "aws_iam_role" "ci_cell_scheduler" {
  name = "homelab-ci-cell-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.ci_account_id
        }
      }
    }]
  })

  tags = { role = "ci" }
}

resource "aws_iam_role_policy" "ci_cell_scheduler" {
  name = "terminate-ci-cells"
  role = aws_iam_role.ci_cell_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ec2:TerminateInstances"
      Resource = "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*"
      Condition = {
        StringEquals = {
          "ec2:ResourceTag/role" = "ci-cell"
        }
      }
    }]
  })
}

# ─── Cell role ───────────────────────────────────────────────────────────────
# Assumed by test-cell jobs (any ref in the project — branch pipelines run
# role tests too). Deny-by-default in shape: RunInstances only through the
# ci-cell launch templates into the CI subnets/SG with the role=ci-cell tag,
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
          "gitlab.com:sub" = "project_path:${local.ci_gitlab_project}:ref_type:*:ref:*"
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
      # RunInstances splits in two statements because aws:RequestTag only
      # evaluates against resources that receive tags in the request
      # (instance + volume, via the launch template tag_specifications);
      # putting it on subnet/SG/AMI ARNs would deny every launch.
      {
        Sid    = "RunTagged"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = [
          "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:instance/*",
          "arn:aws:ec2:${local.ci_aws_region}:${local.ci_account_id}:volume/*",
        ]
        Condition = {
          StringEquals = {
            "aws:RequestTag/role" = "ci-cell"
          }
          ArnEquals = {
            "ec2:LaunchTemplate" = [for lt in aws_launch_template.ci_cell : lt.arn]
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
            aws_key_pair.ci_vagrant.arn,
            "arn:aws:ec2:${local.ci_aws_region}::image/*",
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
      # AMI resolution: our parameters plus Canonical's public ones (minimal).
      # GetParameters (plural) is what EC2 calls server-side for resolve:ssm:
      # image IDs in RunInstances.
      {
        Sid    = "ResolveAmi"
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/ami/*",
          "arn:aws:ssm:${local.ci_aws_region}::parameter/aws/service/canonical/*",
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
          # Build instance lifecycle.
          "ec2:RunInstances", "ec2:StopInstances", "ec2:TerminateInstances",
          "ec2:DescribeInstances", "ec2:DescribeInstanceStatus",
          "ec2:GetConsoleOutput",
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
      # Candidate write + current promotion of /homelab-ci/ami/* parameters.
      {
        Sid    = "PromoteAmi"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters",
          "ssm:DescribeParameters", "ssm:AddTagsToResource",
        ]
        Resource = "arn:aws:ssm:${local.ci_aws_region}:${local.ci_account_id}:parameter/homelab-ci/ami/*"
      },
    ]
  })
}

# ─── Cell SSH key ────────────────────────────────────────────────────────────
# The well-known vagrant insecure keypair (packer/vagrant.key) — the same key
# the qemu fixtures bake and the harness already authenticates with. Public
# by design; the ci_cell security group is the actual boundary. Registered as
# an EC2 keypair so cloud-init on minimal's Canonical AMI (which has no baked
# key) installs it for the ubuntu user.

resource "aws_key_pair" "ci_vagrant" {
  key_name   = "homelab-ci-vagrant"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y spox@vagrant-dev"

  tags = { role = "ci" }
}

# ─── Launch templates ────────────────────────────────────────────────────────
# One per machine. The AMI is deliberately NOT set here: the harness passes
# --image-id resolve:ssm:/homelab-ci/ami/<machine>/<ubuntu> per launch, so a
# bake promotion never touches terraform. Disk layout (multi-disk pug/lab)
# lives in the AMI's block device mapping, not here. Instance types are the
# gate-C tuning knob; t3a sizes mirror the qemu specs' memory for gate B.

locals {
  ci_machine_instance_types = {
    minimal  = "t3a.small"  # 2 GiB, mirrors qemu minimal
    box      = "t3a.medium" # 4 GiB
    box_deps = "t3a.large"  # 8 GiB; box_deps needs >4 GiB (see QEMU_MACHINE_SPECS)
    pug      = "t3a.medium"
    lab      = "t3a.medium"
  }
}

resource "aws_launch_template" "ci_cell" {
  for_each = local.ci_machine_instance_types

  name                   = "ci-cell-${each.key}"
  description            = "homelab CI test cell (${each.key})"
  update_default_version = true

  instance_type = each.value

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

  # Ignored by the baked images (no cloud-init — the vagrant key is baked by
  # chroot.sh), consumed by minimal's Canonical AMI where cloud-init installs
  # it for the ubuntu user. Same key, same SG-as-boundary trust model.
  key_name = aws_key_pair.ci_vagrant.key_name

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
    Name    = "ci-cell-${each.key}"
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
# pipeline, not managed here. resolute has no stable channel yet; add it to
# the map when Canonical publishes one.

data "aws_ssm_parameter" "canonical_ubuntu" {
  for_each = {
    jammy = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
    noble = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
  }
  name = each.value
}

resource "aws_ssm_parameter" "ci_ami_minimal" {
  for_each = data.aws_ssm_parameter.canonical_ubuntu

  name  = "/homelab-ci/ami/minimal/${each.key}"
  type  = "String"
  value = each.value.value

  tags = {
    role    = "ci-ami"
    machine = "minimal"
    ubuntu  = each.key
  }
}

# ─── Budget tripwire ─────────────────────────────────────────────────────────
# Account-wide (the account holds nothing but CI): actual-spend alert at 80%
# and forecast alert at 100% of $10/mo.

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
    notification_type          = "FORECASTED"
    subscriber_email_addresses = ["adrien.kohlbecker@gmail.com"]
  }
}

# ─── Outputs ─────────────────────────────────────────────────────────────────

output "ci_cell_role_arn" {
  description = "AWS_ROLE_ARN for test-cell CI jobs (GitLab OIDC)."
  value       = aws_iam_role.ci_cell.arn
}

output "ci_bake_role_arn" {
  description = "AWS_ROLE_ARN for protected ami_images bake jobs (GitLab OIDC)."
  value       = aws_iam_role.ci_bake.arn
}

output "ci_cell_scheduler_role_arn" {
  description = "Execution role ARN the harness passes to EventBridge Scheduler for the per-cell termination backstop."
  value       = aws_iam_role.ci_cell_scheduler.arn
}

output "ci_subnet_ids" {
  description = "CI VPC public subnet IDs, one per AZ."
  value       = { for k, s in aws_subnet.ci : k => s.id }
}

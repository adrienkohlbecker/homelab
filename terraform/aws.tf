# The personal AWS account (000390721279). It is almost entirely empty: the
# only operator-owned resource is the `ak` admin user below. Everything else is
# AWS-default infra terraform deliberately does NOT adopt --
#   - 3 service-linked roles (license-manager / support / trustedadvisor),
#     created and owned by AWS;
#   - default VPCs in every region.
# There are no EC2 instances, non-default VPCs, S3 buckets, Route53 zones,
# SES/ACM/SNS resources, IAM groups/customer-policies, or anything tagged.
#
# Auth comes from [profile default] in ~/.aws/config (aws-credential-process:
# long-term IAM key + MFA OTP from 1Password -> a 12h STS session). The MinIO
# backend uses [profile minio] instead, so the two never collide and neither
# rides the AWS_* env vars (see main.tf backend + mise.toml).
provider "aws" {
  region  = "eu-central-1"
  profile = "default"
}

# The single human admin. Not managing the access key, virtual MFA device, or
# console login profile attached to it: their secrets/passwords can't be read
# back via the API (they live in 1Password), and adopting the bootstrap access
# key here would let a future recreate break the very credential process that
# authenticates these tofu runs.
resource "aws_iam_user" "ak" {
  name = "ak"
  path = "/"
}

# AdministratorAccess is an AWS-managed policy; attach-by-arn rather than
# defining a customer-managed copy.
resource "aws_iam_user_policy_attachment" "ak_admin" {
  user       = aws_iam_user.ak.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

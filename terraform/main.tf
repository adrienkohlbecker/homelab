terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5"
    }
    gandi = {
      source  = "go-gandi/gandi"
      version = "~> 2"
    }
    nexus = {
      source  = "datadrivers/nexus"
      version = "~> 2"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6"
    }
    mailgun = {
      source  = "wgebis/mailgun"
      version = "~> 0.9"
    }
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.63"
    }
    # Retained transiently to destroy the old Scaleway `fox` resources on the
    # migration apply (a provider can't be dropped while resources it manages
    # still live in state). Remove this + terraform/scaleway.tf once the apply
    # has destroyed them and `tofu state list | grep scaleway` is empty.
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 6"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3"
    }
  }

  required_version = ">= 1.11.4"

  backend "s3" {
    bucket = "terraform"
    key    = "homelab.tfstate"
    region = "us-east-1"
    endpoints = {
      s3 = "https://minio-api.lab.fahm.fr"
    }
    # Static MinIO keys via [profile minio] (minio-credential-process) instead
    # of AWS_* env vars -- that frees the default credential chain for the real
    # `provider "aws"` (account 000390721279, MFA via the default profile). See
    # terraform/aws.tf and ~/.aws/config.
    profile        = "minio"
    use_path_style = true
    use_lockfile   = true
    # MinIO doesn't speak STS or IMDS; without these the AWS provider
    # tries sts:GetCallerIdentity and EC2 metadata probes and fails.
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    # MinIO 2022-05 (pinned in roles/minio) returns 501 on the SDK's
    # default trailing PutObject checksum. Drop once MinIO is upgraded.
    skip_s3_checksum = true
  }

  # PBKDF2-derived key from var.state_passphrase, AES-GCM encrypting
  # state and plan before they leave the process. The MinIO backend
  # stores ciphertext only.
  # Rotation procedure: notes/terraform-state-encryption-rotation.md
  encryption {
    key_provider "pbkdf2" "main" {
      passphrase = var.state_passphrase
    }
    method "aes_gcm" "main" {
      keys = key_provider.pbkdf2.main
    }
    state {
      method   = method.aes_gcm.main
      enforced = true
    }
    plan {
      method   = method.aes_gcm.main
      enforced = true
    }
  }
}

# Resolved from TF_VAR_state_passphrase in mise.toml [env] (1Password
# via op run). Statically evaluated -- read by the encryption block
# above before any state IO.
variable "state_passphrase" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.state_passphrase) > 0
    error_message = "state_passphrase must be non-empty (resolved via TF_VAR_state_passphrase from 1Password through `op run`)."
  }
}


# Resolved from TF_VAR_home_wan_ip in mise.toml [env] (1Password via op
# run). The residential WAN IP, kept out of this public repo.
variable "home_wan_ip" {
  type      = string
  sensitive = true

  validation {
    condition     = length(var.home_wan_ip) > 0
    error_message = "home_wan_ip must be non-empty (resolved via TF_VAR_home_wan_ip from 1Password through `op run`)."
  }
}


# Single human-owned account; pinning the literal avoids one
# resolve-by-name API call per plan.
locals {
  cloudflare_account_id = "43f1339d1841088669b616cecc6562de"

  # Home WAN IP (the Freebox public v4), scoping fox's SSH firewall rule to the
  # home network. Sourced from var.home_wan_ip (TF_VAR via 1Password). Residential,
  # so it can change -- update the 1Password item if it does (and unblock fox SSH
  # via the Hetzner Cloud console in the meantime). The home.fahm.fr A record reads
  # the same var (dns_fahm_fr.tf) via local.fahm_fr_wan_records -- single source now.
  home_wan_ip = var.home_wan_ip

  # data/network_topology.yml is the canonical IP topology, also
  # consumed by ansible (group_vars/all/network.yml — same source of
  # truth, parallel consumer). `network` loads it verbatim;
  # `test_network` applies the same 10.123 → 10.234 gsub that
  # group_vars/test.yml uses, so mhaf.fr (test) DNS records can
  # reference network.* under the test subnet without a parallel
  # data file. path.module = terraform/, so ../data/ is repo-root.
  network      = yamldecode(file("${path.module}/../data/network_topology.yml"))
  test_network = yamldecode(replace(file("${path.module}/../data/network_topology.yml"), "10.123", "10.234"))
}

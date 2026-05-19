terraform {
  required_providers {
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

  # State + plan encryption: PBKDF2 (sha512, 600k iter default) derives a
  # key from var.state_passphrase, AES-GCM encrypts state and plan files
  # before they leave the process. The MinIO backend stores ciphertext
  # only. Every secret tofu manages (mailgun keys, nexus push passwords,
  # gandi PAT, google idp client_secret, etc.) lives in the encrypted
  # state -- compromise of the passphrase + access to the encrypted state
  # recovers all of them.
  #
  # Rotation procedure: see notes/terraform-state-encryption-rotation.md
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

# Sourced from $TF_VAR_state_passphrase in mise.toml [env] (op:// reference
# to 1Password, resolved by op run). Tofu's static evaluation lets the
# encryption block above read it before any state IO. Rotation procedure
# documented above the encryption block.
variable "state_passphrase" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.state_passphrase) > 0
    error_message = "state_passphrase must be non-empty (resolved via TF_VAR_state_passphrase from 1Password through `op run`)."
  }
}


# Single human-owned account; pinning the literal avoids re-resolving by
# name on every plan (one fewer API call, no collision footgun if the
# account is ever renamed or duplicated under a billing migration).
locals {
  cloudflare_account_id = "43f1339d1841088669b616cecc6562de"
}

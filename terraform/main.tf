terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5"
    }
    nexus = {
      source  = "datadrivers/nexus"
      version = "~> 2"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2"
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
# encryption block above read it before any state IO.
variable "state_passphrase" {
  type = string
}


data "cloudflare_account" "main" {
  filter = {
    name = "Home"
  }
}

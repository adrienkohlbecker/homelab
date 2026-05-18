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

  # State + plan encryption posture:
  #   - PBKDF2 (600k iter sha512 by default) derives a key from
  #     var.state_passphrase, AES-GCM encrypts the state and plan files
  #     before they leave the process. The MinIO backend stores
  #     ciphertext; nothing in plaintext touches disk or the network.
  #   - Every secret tofu manages (mailgun keys, nexus push passwords,
  #     gandi PAT, google idp client_secret, etc.) lives in that state
  #     file -- compromise of the passphrase + access to the encrypted
  #     state recovers all of them. Defense in depth:
  #       1. var.state_passphrase is 1Password-generated high-entropy,
  #          never typed; resolved via TF_VAR_state_passphrase in
  #          mise.toml [env] through `op run`.
  #       2. The MinIO `terraform` bucket policy should grant access
  #          to the operator's personal MinIO user only -- not the
  #          homelab_push CI user, not anonymous. Audit periodically;
  #          this is bucket-side config not represented in this repo.
  #
  # Rotating var.state_passphrase (every ~12 months or after suspected
  # 1Password exposure):
  #   1. Generate the new passphrase in 1Password under a temporary
  #      field (e.g. `state_passphrase_new`).
  #   2. Temporarily add a fallback key_provider + method + state/plan
  #      fallback below, sourcing the OLD passphrase from a new
  #      `variable "state_passphrase_old"`; wire it through mise.toml
  #      as TF_VAR_state_passphrase_old pointing at the existing 1P
  #      field. Point the primary at the NEW passphrase.
  #   3. `mise run tf apply` -- tofu re-encrypts state/plans with the
  #      new key in a single transaction.
  #   4. Remove the fallback block + the _old variable + the
  #      mise.toml entry. Update the 1P field to the new value, delete
  #      the temporary field.
  #   5. Verify with a `mise run tf plan` (must produce no diff and
  #      not error on decryption).
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
  type = string
}


data "cloudflare_account" "main" {
  filter = {
    name = "Home"
  }
}

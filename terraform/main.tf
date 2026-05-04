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
    # The passphrase is injected at runtime via TF_ENCRYPTION env (sourced
    # from 1Password through `op run`). The empty value here is overridden
    # by the env merge — tofu refuses to encrypt with an empty key.
    key_provider "pbkdf2" "main" {
      passphrase = ""
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

# data "cloudflare_account" "main" {
#   filter = {
#     name = "Home"
#   }
# }

# resource "cloudflare_zone" "fahm_dev" {
#   account = {
#     id = "${data.cloudflare_account.main.account_id}"
#   }
#   name = "mhaf.fr"
#   type = "full"
# }

# resource "cloudflare_zone" "fahm_fr" {
#   account = {
#     id = "${data.cloudflare_account.main.account_id}"
#   }
#   name = "fahm.fr"
#   type = "full"
# }

# resource "cloudflare_dns_record" "star_box_fahm_dev" {
#   zone_id = "${cloudflare_zone.fahm_dev.id}"
#   content = "box.mhaf.fr"
#   name = "*.box.mhaf.fr"
#   proxied = false
#   ttl = 1
#   type = "CNAME"
# }

# resource "cloudflare_dns_record" "box_fahm_dev" {
#   zone_id = "${cloudflare_zone.fahm_dev.id}"
#   content = "10.234.0.5"
#   name = "box.mhaf.fr"
#   proxied = false
#   ttl = 1
#   type = "A"
# }

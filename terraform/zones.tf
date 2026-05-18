resource "cloudflare_zone" "fahm_fr" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "fahm.fr"
  type = "full"
}

resource "cloudflare_zone" "mhaf_fr" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "mhaf.fr"
  type = "full"
}

resource "cloudflare_zone" "adrienkohlbecker_com" {
  account = {
    id = data.cloudflare_account.main.account_id
  }
  name = "adrienkohlbecker.com"
  type = "full"
}

# DNSSEC: status=active flips CF from "signing capability provisioned"
# to "actually signing the zone" and populates the KSK
# (algorithm/public_key/ds) on the resource. Without status= the
# v5 provider creates the resource but leaves DNSSEC disabled at CF
# -- a silent half-config. terraform/gandi.tf reads these computed
# fields to register the matching DS at Gandi-as-registrar, closing
# the chain from the root. Removing here without first removing the
# Gandi-side DS breaks resolution for the zone.
resource "cloudflare_zone_dnssec" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
  status  = "active"
}

resource "cloudflare_zone_dnssec" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
  status  = "active"
}

resource "cloudflare_zone_dnssec" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  status  = "active"
}

# Canonical zone-name → id map for use across surfaces that fan out
# across all zones (CAA records, zone settings, web analytics).
locals {
  zones = {
    "fahm.fr"              = cloudflare_zone.fahm_fr.id
    "mhaf.fr"              = cloudflare_zone.mhaf_fr.id
    "adrienkohlbecker.com" = cloudflare_zone.adrienkohlbecker_com.id
  }
}

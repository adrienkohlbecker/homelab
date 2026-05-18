resource "cloudflare_zone" "fahm_fr" {
  account = {
    id = local.cloudflare_account_id
  }
  name = "fahm.fr"
  type = "full"
}

resource "cloudflare_zone" "mhaf_fr" {
  account = {
    id = local.cloudflare_account_id
  }
  name = "mhaf.fr"
  type = "full"
}

resource "cloudflare_zone" "adrienkohlbecker_com" {
  account = {
    id = local.cloudflare_account_id
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
# the chain from the root.
#
# prevent_destroy is on each of these because `tofu destroy` has no
# wait-for-TTL primitive: with both halves of the chain in one plan,
# the implicit dependency order (gandi_dnssec_key.X depends on
# cloudflare_zone_dnssec.X) means destroy removes Gandi DS first, then
# immediately flips CF to inactive -- but the parent's DS TTL is still
# being served (registry-dependent, typically 5min-7days), so during
# that window the zone is `bogus` to validating resolvers (DS pointing
# at a KSK that no longer signs). lab.fahm.fr / mail.fahm.fr / SMTP
# delivery all silently break at 1.1.1.1 / 8.8.8.8 / any validating
# ISP resolver.
#
# To actually retire DNSSEC on a zone:
#   1. `tofu destroy -target=gandi_dnssec_key.<zone>` (Gandi side first).
#   2. Wait for the parent's DS TTL to expire (use `dig +trace +dnssec
#      <zone>` against the parent NS; the SOA's minimum TTL is the
#      upper bound for the DS TTL).
#   3. Remove the `lifecycle { prevent_destroy = true }` line from the
#      cloudflare_zone_dnssec.<zone> resource below.
#   4. `tofu apply` (or `tofu destroy -target=cloudflare_zone_dnssec.<zone>`)
#      to flip CF to inactive.
#   5. Delete the resource block + the matching gandi_dnssec_key block.
resource "cloudflare_zone_dnssec" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
  status  = "active"

  lifecycle { prevent_destroy = true }
}

resource "cloudflare_zone_dnssec" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
  status  = "active"

  lifecycle { prevent_destroy = true }
}

resource "cloudflare_zone_dnssec" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  status  = "active"

  lifecycle { prevent_destroy = true }
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

# Both halves of the DNSSEC chain must be active for the zone to validate.
# Tautology-by-construction today (Gandi DS is built from CF's KSK fields),
# but catches the future case where someone disables CF DNSSEC or deletes
# the Gandi DS through the UI between applies -- without this, the next
# `tofu plan` would silently reconcile the drift, leaving the zone bogus
# to validating resolvers for the DS TTL window.
check "dnssec_chain" {
  assert {
    condition     = cloudflare_zone_dnssec.fahm_fr.status == "active" && gandi_dnssec_key.fahm_fr.id != null
    error_message = "DNSSEC chain broken for fahm.fr: CF status=${cloudflare_zone_dnssec.fahm_fr.status}, Gandi DS=${gandi_dnssec_key.fahm_fr.id == null ? "missing" : "present"}. Zone is bogus to validating resolvers."
  }
  assert {
    condition     = cloudflare_zone_dnssec.mhaf_fr.status == "active" && gandi_dnssec_key.mhaf_fr.id != null
    error_message = "DNSSEC chain broken for mhaf.fr: CF status=${cloudflare_zone_dnssec.mhaf_fr.status}, Gandi DS=${gandi_dnssec_key.mhaf_fr.id == null ? "missing" : "present"}. Zone is bogus to validating resolvers."
  }
  assert {
    condition     = cloudflare_zone_dnssec.adrienkohlbecker_com.status == "active" && gandi_dnssec_key.adrienkohlbecker_com.id != null
    error_message = "DNSSEC chain broken for adrienkohlbecker.com: CF status=${cloudflare_zone_dnssec.adrienkohlbecker_com.status}, Gandi DS=${gandi_dnssec_key.adrienkohlbecker_com.id == null ? "missing" : "present"}. Zone is bogus to validating resolvers."
  }
}

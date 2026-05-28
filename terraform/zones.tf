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

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status]   # CF flaps pending↔active; don't let it churn the Gandi DS
  }
}

resource "cloudflare_zone_dnssec" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
  status  = "active"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status]   # CF flaps pending↔active; don't let it churn the Gandi DS
  }
}

resource "cloudflare_zone_dnssec" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  status  = "active"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status]   # CF flaps pending↔active; don't let it churn the Gandi DS
  }
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

# The CF-side half of the DNSSEC chain is asserted as a precondition on
# each gandi_dnssec_key.X in gandi.tf -- if CF DNSSEC gets flipped to
# inactive via UI between applies, the precondition halts `tofu plan`
# instead of warning (as a `check` block would). A precondition is the
# right primitive here because it can read another resource's post-refresh
# attribute (cloudflare_zone_dnssec.X.status), unlike `self` in a
# precondition which only sees configured values.

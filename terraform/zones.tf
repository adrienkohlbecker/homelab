locals {
  zone_names = toset(["adrienkohlbecker.com", "fahm.fr", "mhaf.fr"])
}

resource "cloudflare_zone" "this" {
  for_each = local.zone_names

  account = {
    id = local.cloudflare_account_id
  }
  name = each.key
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
# the implicit dependency order (gandi_dnssec_key.this depends on the
# cloudflare_zone_dnssec resources) means destroy removes Gandi DS first, then
# immediately flips CF to inactive -- but the parent's DS TTL is still
# being served (registry-dependent, typically 5min-7days), so during
# that window the zone is `bogus` to validating resolvers (DS pointing
# at a KSK that no longer signs). lab.fahm.fr / mail.fahm.fr / SMTP
# delivery all silently break at 1.1.1.1 / 8.8.8.8 / any validating
# ISP resolver.
#
# To actually retire DNSSEC on a zone:
#   1. `tofu destroy -target='gandi_dnssec_key.this["<zone>"]'` (Gandi first).
#   2. Wait for the parent's DS TTL to expire (use `dig +trace +dnssec
#      <zone>` against the parent NS; the SOA's minimum TTL is the
#      upper bound for the DS TTL).
#   3. Remove `prevent_destroy` from that zone's cloudflare_zone_dnssec resource.
#   4. `tofu apply` (or `tofu destroy -target=cloudflare_zone_dnssec.<zone>`)
#      to flip CF to inactive.
#   5. Delete that DNSSEC resource block and its local.zone_dnssec entry.
# These remain separate so step 3 never exposes another zone to destruction.
resource "cloudflare_zone_dnssec" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.this["adrienkohlbecker.com"].id
  status  = "active"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status] # CF flaps pending↔active; don't let it churn the Gandi DS
  }
}

resource "cloudflare_zone_dnssec" "fahm_fr" {
  zone_id = cloudflare_zone.this["fahm.fr"].id
  status  = "active"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status] # CF flaps pending↔active; don't let it churn the Gandi DS
  }
}

resource "cloudflare_zone_dnssec" "mhaf_fr" {
  zone_id = cloudflare_zone.this["mhaf.fr"].id
  status  = "active"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status] # CF flaps pending↔active; don't let it churn the Gandi DS
  }
}

# Canonical zone-name → id map for use across surfaces that fan out
# across all zones (CAA records, zone settings, web analytics).
locals {
  zones = { for name, zone in cloudflare_zone.this : name => zone.id }
  zone_dnssec = {
    "adrienkohlbecker.com" = cloudflare_zone_dnssec.adrienkohlbecker_com
    "fahm.fr"              = cloudflare_zone_dnssec.fahm_fr
    "mhaf.fr"              = cloudflare_zone_dnssec.mhaf_fr
  }
}

# The CF-side half of the DNSSEC chain is asserted as a precondition on
# gandi_dnssec_key.this in gandi.tf -- if CF DNSSEC gets disabled via
# the UI between applies, the precondition halts `tofu plan` instead of
# warning (as a `check` block would). It accepts status in
# {active, pending} and rejects only the disabled states: CF parks a
# healthy zone at "pending" forever when it can't auto-confirm the DS
# at a third-party registrar (see the long note in gandi.tf), so an
# `== "active"` test would false-halt every plan. A precondition is the
# right primitive here because it can read another resource's post-refresh
# attribute (the cloudflare_zone_dnssec status), unlike `self` in a
# precondition which only sees configured values.

moved {
  from = cloudflare_zone.adrienkohlbecker_com
  to   = cloudflare_zone.this["adrienkohlbecker.com"]
}

moved {
  from = cloudflare_zone.fahm_fr
  to   = cloudflare_zone.this["fahm.fr"]
}

moved {
  from = cloudflare_zone.mhaf_fr
  to   = cloudflare_zone.this["mhaf.fr"]
}

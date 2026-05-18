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

# DNSSEC: enabling at CF generates a DS record. The DS still has to be
# pasted into Gandi (the registrar) per zone for validation to chain
# from the root -- TF can't reach Gandi. Until the DS is registered,
# CF signs records but resolvers don't validate. Removing DNSSEC here
# without first removing the DS at Gandi would break resolution.
resource "cloudflare_zone_dnssec" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
}

resource "cloudflare_zone_dnssec" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_zone_dnssec" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
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

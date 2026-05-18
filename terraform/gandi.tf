# Gandi is the registrar for fahm.fr / mhaf.fr / adrienkohlbecker.com.
# Authoritative DNS lives at Cloudflare; Gandi-side we manage the
# surfaces that close the loop between registrar and DNS host:
# nameserver delegation and the DNSSEC DS chain.
#
# Auth uses a Gandi Personal Access Token (the old API key was
# deprecated in 2024). Provisioned in the Gandi UI under
# Account > Personal Access Tokens, scoped to the organization that
# owns these 3 domains. Stored in 1Password and surfaced via the
# standard TF_VAR_gandi_pat mechanism in mise.toml [env].
#
# Surfaces intentionally NOT under tofu:
#
# - gandi_domain (registration metadata: contacts, autorenew, tags).
#   The provider's Read flattens contacts back from the WHOIS
#   response, which Gandi serves obfuscated when mail_obfuscated=true
#   (current setting). HCL would have to declare full PII (city,
#   street_addr, phone, zip) but state reads back the obfuscated
#   version -> perpetual plan drift. Worse, the provider's Update
#   explicitly refuses owner-contact changes ("currently not
#   supported"), so we couldn't fix the drift even if we wanted
#   to. UI-managed is the only sane option until the provider
#   matures.
#
# - fahm.dev: lapsed 2026-04-17, in clientHold at the registry,
#   autorenew off. Will release back to the pool in due course; not
#   worth bringing under tofu.
#
# - mailboxes / email forwardings / glue records / livedns records:
#   all empty across all 4 domains (email goes through CF Email
#   Routing + Fastmail/Gmail; DNS at CF). If any of these grow,
#   gandi_mailbox / gandi_email_forwarding / gandi_glue_record /
#   gandi_livedns_record are the resources to reach for.

variable "gandi_pat" {
  type      = string
  sensitive = true

  validation {
    condition     = length(var.gandi_pat) > 0
    error_message = "gandi_pat must be non-empty (resolved via TF_VAR_gandi_pat from 1Password through `op run`)."
  }
}

provider "gandi" {
  personal_access_token = var.gandi_pat
}

# Pin the registrar-side NS delegation to whatever CF assigned the
# zone (emma/eric.ns.cloudflare.com today). Reading from
# cloudflare_zone.X.name_servers means a CF-side NS rotation (rare
# but documented) flows through with a single tofu apply rather
# than a manual UI fix at Gandi.
resource "gandi_nameservers" "fahm_fr" {
  domain      = "fahm.fr"
  nameservers = cloudflare_zone.fahm_fr.name_servers
}

resource "gandi_nameservers" "mhaf_fr" {
  domain      = "mhaf.fr"
  nameservers = cloudflare_zone.mhaf_fr.name_servers
}

resource "gandi_nameservers" "adrienkohlbecker_com" {
  domain      = "adrienkohlbecker.com"
  nameservers = cloudflare_zone.adrienkohlbecker_com.name_servers
}

# DNSSEC DS registration. gandi_dnssec_key uploads the KSK material
# to Gandi-as-registrar; Gandi computes the DS and publishes it via
# its parent NS. The chain is only complete once both halves are
# active: CF signs records (cloudflare_zone_dnssec.X.status="active"
# in zones.tf), and Gandi advertises a DS pointing at the matching
# KSK (here). Breaking either half alone leaves the zone bogus to
# validating resolvers -- see the retirement procedure in zones.tf,
# above the cloudflare_zone_dnssec resources.
#
# CF returns algorithm as a stringified number ("13" for
# ECDSAP256SHA256); gandi wants a Number, hence tonumber().
resource "gandi_dnssec_key" "fahm_fr" {
  domain     = "fahm.fr"
  algorithm  = tonumber(cloudflare_zone_dnssec.fahm_fr.algorithm)
  public_key = cloudflare_zone_dnssec.fahm_fr.public_key
  type       = "ksk"
}

resource "gandi_dnssec_key" "mhaf_fr" {
  domain     = "mhaf.fr"
  algorithm  = tonumber(cloudflare_zone_dnssec.mhaf_fr.algorithm)
  public_key = cloudflare_zone_dnssec.mhaf_fr.public_key
  type       = "ksk"
}

resource "gandi_dnssec_key" "adrienkohlbecker_com" {
  domain     = "adrienkohlbecker.com"
  algorithm  = tonumber(cloudflare_zone_dnssec.adrienkohlbecker_com.algorithm)
  public_key = cloudflare_zone_dnssec.adrienkohlbecker_com.public_key
  type       = "ksk"
}

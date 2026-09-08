# Gandi is the registrar for fahm.fr / mhaf.fr / adrienkohlbecker.com.
# Authoritative DNS lives at Cloudflare; Gandi-side we manage the
# surfaces that close the loop between registrar and DNS host:
# nameserver delegation and the DNSSEC DS chain.
#
# Auth uses a Gandi Personal Access Token (the old API key was
# deprecated in 2024). Provisioned in the Gandi UI under
# Account > Personal Access Tokens, scoped to the organization that
# owns these 3 domains. Stored in 1Password and surfaced through
# GANDI_PERSONAL_ACCESS_TOKEN in mise.toml [env].
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
# - mailboxes / email forwardings / glue records / livedns records:
#   all empty across the 3 domains (mail is hosted outside Gandi;
#   DNS is at CF). If any of these grow,
#   gandi_mailbox / gandi_email_forwarding / gandi_glue_record /
#   gandi_livedns_record are the resources to reach for.

# Pin the registrar-side NS delegation to whatever CF assigned the
# zone (emma/eric.ns.cloudflare.com today). Reading from
# the keyed Cloudflare zone resources means a CF-side NS rotation (rare but
# documented) flows through with a single tofu apply rather than a manual UI
# fix at Gandi.
resource "gandi_nameservers" "this" {
  for_each = cloudflare_zone.this

  domain      = each.key
  nameservers = each.value.name_servers
}

# DNSSEC DS registration. gandi_dnssec_key uploads the KSK material
# to Gandi-as-registrar; Gandi computes the DS and publishes it via
# its parent NS. The chain is complete once CF signs the zone and
# Gandi advertises a DS pointing at the matching KSK (here). Breaking
# either half leaves the zone bogus to validating resolvers -- see the
# retirement procedure in zones.tf, above the cloudflare_zone_dnssec
# resources.
#
# CF reports status="pending" (not "active") whenever it can't
# auto-confirm the DS at the parent -- the steady state for a
# third-party registrar like Gandi, since CF never sees a DS it didn't
# place itself. So status sits at "pending" indefinitely even though
# the zone validates fine (dig +dnssec <zone> @1.1.1.1 shows the `ad`
# flag). The precondition therefore guards against the states that
# mean signing is actually OFF at CF (disabled / pending-disabled,
# e.g. flipped via the UI) -- it must NOT reject "pending", or every
# plan halts on a healthy chain. zones.tf pins ignore_changes on
# status for the same reason: the active<->pending flap is cosmetic.
#
# CF returns algorithm as a stringified number ("13" for
# ECDSAP256SHA256); gandi wants a Number, hence tonumber().
resource "gandi_dnssec_key" "this" {
  for_each = local.zone_dnssec

  domain     = each.key
  algorithm  = tonumber(each.value.algorithm)
  public_key = each.value.public_key
  type       = "ksk"

  lifecycle {
    precondition {
      condition     = contains(["active", "pending"], each.value.status)
      error_message = "DNSSEC disabled at CF for ${each.key} (status=${each.value.status}). Re-enable CF signing before touching the Gandi DS, or the zone goes bogus to validating resolvers."
    }
  }
}

# HSTS is enabled only on the zone that actually proxies traffic through
# CF: adrienkohlbecker.com (apex + www). On unproxied zones HSTS has no
# effect *today*, but the moment a future subdomain starts proxying,
# include_subdomains=true + max_age=1y would pin every subdomain on the
# apex to HTTPS-only for a year -- including internal lab subdomains
# (box/bunk/pug/lab.fahm.fr -> RFC1918) that serve plain HTTP. Re-enable
# per-zone deliberately when that zone's subdomain landscape is all-HTTPS.
#
# Keeping all 3 zones in for_each (rather than filtering down to the one
# proxied zone) so the disabled state is *asserted* in CF, not just
# untracked by tofu -- cloudflare_zone_setting destroys remove the
# resource from state without resetting the underlying value at CF.
#
# Bot Fight Mode lives on the /zones/<id>/bot_management endpoint
# (cloudflare_bot_management), NOT here -- "bot_fight_mode" is not a
# valid zone-setting name. Deferred along with the rest of the
# Bot Management surface (apply token doesn't carry Bot Management
# scope; the impact is low enough on a static github-pages site
# that we leave it UI-managed).

resource "cloudflare_zone_setting" "security_header" {
  for_each = local.zones

  zone_id    = each.value
  setting_id = "security_header"
  value = {
    strict_transport_security = each.key == "adrienkohlbecker.com" ? {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      nosniff            = true
      preload            = false
      } : {
      enabled            = false
      max_age            = 0
      include_subdomains = false
      nosniff            = false
      preload            = false
    }
  }
}

# Scalar settings pinned on the one proxied zone. Most already at these
# values today; pinning makes a future CF account-default rollout (which
# happens periodically) visible as a plan diff instead of silent drift.
# min_tls_version is the one actual behavior change: 1.0 -> 1.2.
# fahm.fr / mhaf.fr aren't pinned because they don't proxy traffic --
# revisit per-zone when those zones start proxying (ssl=strict in
# particular would conflict with an un-TLS'd RFC1918 origin).
locals {
  proxied_scalars = {
    always_use_https         = "on"
    automatic_https_rewrites = "on"
    min_tls_version          = "1.2"
    tls_1_3                  = "on"
    http3                    = "on"
    brotli                   = "on"
    opportunistic_encryption = "on"
    ssl                      = "strict" # CF<->origin; github.io serves valid certs
    "0rtt"                   = "off"    # TLS 1.3 0-RTT off by default; pin explicit
  }
}

resource "cloudflare_zone_setting" "proxied_scalars" {
  for_each = local.proxied_scalars

  zone_id    = local.zones["adrienkohlbecker.com"]
  setting_id = each.key
  value      = each.value
}

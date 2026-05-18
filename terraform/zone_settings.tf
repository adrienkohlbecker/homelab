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

locals {
  hsts_zones = toset(["adrienkohlbecker.com"])
}

resource "cloudflare_zone_setting" "security_header" {
  for_each = local.zones

  zone_id    = each.value
  setting_id = "security_header"
  value = {
    strict_transport_security = contains(local.hsts_zones, each.key) ? {
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

# Zone settings applied uniformly to all 3 zones. These only have
# behavioural effect on proxied records -- right now that's
# adrienkohlbecker.com (apex + www), and zero records on fahm.fr or
# mhaf.fr. Setting them anyway so when those zones eventually proxy
# something, the protection auto-engages without a follow-up tofu run.
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
    strict_transport_security = {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      nosniff            = true
      preload            = false
    }
  }
}

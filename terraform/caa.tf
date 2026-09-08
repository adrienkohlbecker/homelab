# CAA: constrain which CAs can issue certs for each zone.
#
# adrienkohlbecker.com (proxied) gets its edge cert from CF's
# Universal SSL pipeline, which rotates across a fixed CA pool. The
# allowlist below mirrors that pool so CF can rotate freely without
# the next renewal getting CAA-blocked. fahm.fr and mhaf.fr aren't
# proxied so CF doesn't issue certs there, but the same allowlist
# applies as a defense against any external CA being asked to
# mis-issue against these domains.
#
# iodef tells the world to ping the inbox if any CA gets asked to
# issue against policy.

locals {
  caa_issuers = ["letsencrypt.org", "pki.goog"]

  caa_authorizations = {
    for authorization in setproduct(keys(local.zones), ["issue", "issuewild"], local.caa_issuers) :
    "${authorization[0]}/${authorization[1]}/${authorization[2]}" => {
      zone_name = authorization[0]
      tag       = authorization[1]
      ca        = authorization[2]
    }
  }
}

resource "cloudflare_dns_record" "caa_authorization" {
  for_each = local.caa_authorizations

  zone_id = local.zones[each.value.zone_name]
  type    = "CAA"
  name    = each.value.zone_name
  ttl     = 1
  data = {
    flags = 0
    tag   = each.value.tag
    value = each.value.ca
  }
}

resource "cloudflare_dns_record" "caa_iodef" {
  for_each = local.zones

  zone_id = each.value
  type    = "CAA"
  name    = each.key
  ttl     = 1
  data = {
    flags = 0
    tag   = "iodef"
    value = "mailto:adrien.kohlbecker@gmail.com"
  }
}

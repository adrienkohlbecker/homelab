# DNS records for adrienkohlbecker.com.
# Apex + www both CNAME to a github-pages origin (proxied through CF for
# Universal SSL). Single keybase verification TXT.

locals {
  adrienkohlbecker_com_records = {
    cname_apex  = { type = "CNAME", name = "adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    cname_www   = { type = "CNAME", name = "www.adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    txt_keybase = { type = "TXT", name = "adrienkohlbecker.com", content = "keybase-site-verification=ARwVSN_9cTudAafXA22PN2Iy7d17v6BHeEwjUPqth6M" }
  }
}

resource "cloudflare_dns_record" "adrienkohlbecker_com" {
  for_each = local.adrienkohlbecker_com_records

  zone_id  = local.zones["adrienkohlbecker.com"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
  tags     = try(each.value.tags, [])
}

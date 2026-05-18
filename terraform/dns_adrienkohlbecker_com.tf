# DNS records for adrienkohlbecker.com.
# Apex + www both CNAME to a github-pages origin (proxied through CF for
# Universal SSL). Single keybase verification TXT.
# See dns_fahm_fr.tf for the map key shape.

locals {
  adrienkohlbecker_com_records = {
    "CNAME/adrienkohlbecker.com"     = { content = "adrienkohlbecker.github.io", proxied = true }
    "CNAME/www.adrienkohlbecker.com" = { content = "adrienkohlbecker.github.io", proxied = true }
    "TXT/adrienkohlbecker.com"       = { content = "keybase-site-verification=ARwVSN_9cTudAafXA22PN2Iy7d17v6BHeEwjUPqth6M" }
  }
}

resource "cloudflare_dns_record" "adrienkohlbecker_com" {
  for_each = local.adrienkohlbecker_com_records

  zone_id  = local.zones["adrienkohlbecker.com"]
  type     = split("/", each.key)[0]
  name     = split("/", each.key)[1]
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
}

# ---- State migrations ----

moved {
  from = cloudflare_dns_record.cname["adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["CNAME/adrienkohlbecker.com"]
}
moved {
  from = cloudflare_dns_record.cname["www.adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["CNAME/www.adrienkohlbecker.com"]
}
moved {
  from = cloudflare_dns_record.txt["adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["TXT/adrienkohlbecker.com"]
}

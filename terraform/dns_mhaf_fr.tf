# DNS records for mhaf.fr.
# See dns_fahm_fr.tf for the map key shape and the DMARC staging plan
# (same posture applies here -- currently p=none, observation only).
#
# echo.mhaf.fr is proxied so CF intercepts and applies the `echo` Access
# policies (see access.tf). The CNAME target is arbitrary -- CF gates the
# request before it reaches the origin; for this test fixture the origin
# doesn't need to respond.

locals {
  mhaf_fr_records = {
    # A
    "A/box.mhaf.fr"   = { content = "10.234.0.5" }
    "A/*.lab.mhaf.fr" = { content = "10.234.0.2" }

    # CNAME
    "CNAME/*.box.mhaf.fr"          = { content = "box.mhaf.fr" }
    "CNAME/echo.mhaf.fr"           = { content = "box.mhaf.fr", proxied = true }
    "CNAME/fm1._domainkey.mhaf.fr" = { content = "fm1.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    "CNAME/fm2._domainkey.mhaf.fr" = { content = "fm2.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    "CNAME/fm3._domainkey.mhaf.fr" = { content = "fm3.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }

    # TXT
    "TXT/_dmarc.mhaf.fr" = { content = "v=DMARC1; p=none;", comment = "fastmail" }
    "TXT/mhaf.fr"        = { content = "v=spf1 include:spf.messagingengine.com ?all", comment = "fastmail" }

    # MX
    "MX/mhaf.fr/in1-smtp.messagingengine.com"   = { content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "MX/mhaf.fr/in2-smtp.messagingengine.com"   = { content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    "MX/*.mhaf.fr/in1-smtp.messagingengine.com" = { content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "MX/*.mhaf.fr/in2-smtp.messagingengine.com" = { content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
  }
}

resource "cloudflare_dns_record" "mhaf_fr" {
  for_each = local.mhaf_fr_records

  zone_id  = local.zones["mhaf.fr"]
  type     = split("/", each.key)[0]
  name     = split("/", each.key)[1]
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
}

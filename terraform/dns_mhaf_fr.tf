# DNS records for mhaf.fr.
# See dns_fahm_fr.tf for the DMARC staging plan; mhaf.fr currently stays
# at p=none, observation only.
#
# echo.mhaf.fr is proxied so CF intercepts and applies the `echo` Access
# policies (see access.tf). The CNAME target is arbitrary -- CF gates the
# request before it reaches the origin; for this test fixture the origin
# doesn't need to respond.

locals {
  mhaf_fr_records = {
    # A — test hosts derive from the 10.234.x view of the shared topology.
    a_lab          = { type = "A", name = "lab.mhaf.fr", content = local.test_network.hosts.lab.physical }
    a_wildcard_lab = { type = "A", name = "*.lab.mhaf.fr", content = local.test_network.hosts.lab.physical }

    # CNAME
    cname_echo          = { type = "CNAME", name = "echo.mhaf.fr", content = "lab.mhaf.fr", proxied = true }
    cname_fm1_domainkey = { type = "CNAME", name = "fm1._domainkey.mhaf.fr", content = "fm1.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    cname_fm2_domainkey = { type = "CNAME", name = "fm2._domainkey.mhaf.fr", content = "fm2.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    cname_fm3_domainkey = { type = "CNAME", name = "fm3._domainkey.mhaf.fr", content = "fm3.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }

    # TXT
    txt_dmarc = { type = "TXT", name = "_dmarc.mhaf.fr", content = "v=DMARC1; p=none;", comment = "fastmail" }
    txt_spf   = { type = "TXT", name = "mhaf.fr", content = "v=spf1 include:spf.messagingengine.com ?all", comment = "fastmail" }

    # MX
    mx_mhaf_fr_in1  = { type = "MX", name = "mhaf.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    mx_mhaf_fr_in2  = { type = "MX", name = "mhaf.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    mx_wildcard_in1 = { type = "MX", name = "*.mhaf.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    mx_wildcard_in2 = { type = "MX", name = "*.mhaf.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
  }
}

resource "cloudflare_dns_record" "mhaf_fr" {
  for_each = local.mhaf_fr_records

  zone_id  = local.zones["mhaf.fr"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
}

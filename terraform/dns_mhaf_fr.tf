# DNS records for mhaf.fr.
# See dns_fahm_fr.tf for the variable shape and the DMARC staging plan
# (same posture applies here -- currently p=none, observation only).
#
# echo.mhaf.fr is proxied so CF intercepts and applies the `echo` Access
# policies (see access.tf). The CNAME target is arbitrary -- CF gates the
# request before it reaches the origin; for this test fixture the origin
# doesn't need to respond.

variable "mhaf_fr_records" {
  description = "DNS records (A/CNAME/TXT/MX) for mhaf.fr."
  type = map(object({
    type     = string
    name     = string
    content  = string
    priority = optional(number)
    proxied  = optional(bool, false)
    comment  = optional(string)
    tags     = optional(list(string), [])
  }))

  validation {
    condition     = alltrue([for r in var.mhaf_fr_records : contains(["A", "CNAME", "TXT", "MX"], r.type)])
    error_message = "type must be one of A, CNAME, TXT, MX."
  }

  default = {
    # A
    a_box          = { type = "A", name = "box.mhaf.fr", content = "10.234.0.5" }
    a_wildcard_lab = { type = "A", name = "*.lab.mhaf.fr", content = "10.234.0.2" }

    # CNAME
    cname_wildcard_box  = { type = "CNAME", name = "*.box.mhaf.fr", content = "box.mhaf.fr" }
    cname_echo          = { type = "CNAME", name = "echo.mhaf.fr", content = "box.mhaf.fr", proxied = true }
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
  for_each = var.mhaf_fr_records

  zone_id  = local.zones["mhaf.fr"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = each.value.priority
  proxied  = each.value.proxied
  ttl      = 1
  comment  = each.value.comment
  tags     = each.value.tags
}

# State migration from the previous "<TYPE>/<name>[/<content>]" key shape.
# Safe to prune once `tofu apply` confirms a no-op plan.
moved {
  from = cloudflare_dns_record.mhaf_fr["A/box.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["a_box"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["A/*.lab.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["a_wildcard_lab"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["CNAME/*.box.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["cname_wildcard_box"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["CNAME/echo.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["cname_echo"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["CNAME/fm1._domainkey.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["cname_fm1_domainkey"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["CNAME/fm2._domainkey.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["cname_fm2_domainkey"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["CNAME/fm3._domainkey.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["cname_fm3_domainkey"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["TXT/_dmarc.mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["txt_dmarc"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["TXT/mhaf.fr"]
  to   = cloudflare_dns_record.mhaf_fr["txt_spf"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["MX/mhaf.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.mhaf_fr["mx_mhaf_fr_in1"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["MX/mhaf.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.mhaf_fr["mx_mhaf_fr_in2"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["MX/*.mhaf.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.mhaf_fr["mx_wildcard_in1"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr["MX/*.mhaf.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.mhaf_fr["mx_wildcard_in2"]
}

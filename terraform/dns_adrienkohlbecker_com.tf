# DNS records for adrienkohlbecker.com.
# Apex + www both CNAME to a github-pages origin (proxied through CF for
# Universal SSL). Single keybase verification TXT.
# See dns_fahm_fr.tf for the variable shape.

variable "adrienkohlbecker_com_records" {
  description = "DNS records (A/CNAME/TXT/MX) for adrienkohlbecker.com."
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
    condition     = alltrue([for r in var.adrienkohlbecker_com_records : contains(["A", "CNAME", "TXT", "MX"], r.type)])
    error_message = "type must be one of A, CNAME, TXT, MX."
  }

  default = {
    cname_apex  = { type = "CNAME", name = "adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    cname_www   = { type = "CNAME", name = "www.adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    txt_keybase = { type = "TXT", name = "adrienkohlbecker.com", content = "keybase-site-verification=ARwVSN_9cTudAafXA22PN2Iy7d17v6BHeEwjUPqth6M" }
  }
}

resource "cloudflare_dns_record" "adrienkohlbecker_com" {
  for_each = var.adrienkohlbecker_com_records

  zone_id  = local.zones["adrienkohlbecker.com"]
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
  from = cloudflare_dns_record.adrienkohlbecker_com["CNAME/adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["cname_apex"]
}
moved {
  from = cloudflare_dns_record.adrienkohlbecker_com["CNAME/www.adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["cname_www"]
}
moved {
  from = cloudflare_dns_record.adrienkohlbecker_com["TXT/adrienkohlbecker.com"]
  to   = cloudflare_dns_record.adrienkohlbecker_com["txt_keybase"]
}

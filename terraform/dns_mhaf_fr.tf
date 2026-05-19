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
    # A — host records derive from data/network_topology.yml via
    # `local.mhaf_fr_host_records` below (using `local.test_network`
    # for the 10.123 → 10.234 gsub).

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

locals {
  # Host A records for mhaf.fr (the test zone) — derive from
  # `local.test_network` (the gsub'd 10.234.x view of the topology).
  # Names resolve to the host's physical IP in the test environment,
  # matching what group_vars/test.yml builds for external_ips.
  mhaf_fr_host_records = {
    a_box          = { type = "A", name = "box.mhaf.fr", content = local.test_network.hosts.box.physical }
    a_wildcard_lab = { type = "A", name = "*.lab.mhaf.fr", content = local.test_network.hosts.lab.physical }
  }
  mhaf_fr_records = merge(var.mhaf_fr_records, local.mhaf_fr_host_records)
}

resource "cloudflare_dns_record" "mhaf_fr" {
  for_each = local.mhaf_fr_records

  # See dns_fahm_fr.tf for the rationale on `try()` here — the
  # merge between var.mhaf_fr_records (typed) and
  # local.mhaf_fr_host_records (untyped) leaves merged entries
  # without the optional default fill-ins.
  zone_id  = local.zones["mhaf.fr"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
  tags     = try(each.value.tags, [])
}

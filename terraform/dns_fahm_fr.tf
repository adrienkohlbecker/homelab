# DNS records for fahm.fr.
#
# Records (A/CNAME/TXT/MX) live in a single typed variable; one
# cloudflare_dns_record resource iterates it. SRV records sit in their
# own map + resource because they carry a nested data {} block rather
# than a flat content string.
#
# Map entries are keyed by an opaque slug (e.g. a_box, mx_fahm_fr_in1)
# and carry type/name/content explicitly in the value, so the resource
# body is a thin pass-through with no key parsing. Adding a record:
# pick a slug that doesn't collide and fill in type/name/content.
#
# The CF Email Routing outbound DKIM TXT (cf2024-1._domainkey.fahm.fr)
# needs lifecycle { ignore_changes = [content] } because CF auto-rotates
# the key server-side. It stays as a standalone resource at the bottom of
# this file -- lifecycle blocks can't reference each.value.

# ---- A/CNAME/TXT/MX records ----
#
# DMARC posture is currently p=none across all three zones (observation
# only -- failing mail is delivered but flagged). Staged enforcement:
#
#   Stage 1 (now): p=none with rua=...; collect 2 weeks of aggregate
#     reports per zone. Targets are operator inboxes
#     (dmarc-reports.cloudflare.net for fahm.fr; mailgun + ondmarc for
#     noreply). Stay at this stage until the report stream shows only
#     legitimate senders -- Fastmail (in*-smtp.messagingengine.com) +
#     Mailgun (eu.mailgun.org) for fahm.fr + noreply.fahm.fr.
#
#   Stage 2 (after clean reports): p=quarantine; pct=25 for one week,
#     then pct=100. Tighten SPF to -all (hardfail) at the same time.
#
#   Stage 3 (production): p=reject. Forgeries with From: <user>@<zone>
#     get rejected by the receiver, eliminating the phishing-by-spoof
#     vector that p=none allows today.
#
# Homelab framing: spear-phishing the operator's contacts via spoofed
# fahm.fr From: headers is the abuse case; for the noreply Mailgun-
# sending zone the rollout is more clearly justified since every
# legitimate sender there is already DKIM-signing through pdk1/pdk2
# selectors.

variable "fahm_fr_records" {
  description = "DNS records (A/CNAME/TXT/MX) for fahm.fr. SRV records live in local.fahm_fr_srv_records."
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
    condition     = alltrue([for r in var.fahm_fr_records : contains(["A", "CNAME", "TXT", "MX"], r.type)])
    error_message = "type must be one of A, CNAME, TXT, MX (SRV records belong in fahm_fr_srv_records)."
  }

  default = {
    # A
    a_box  = { type = "A", name = "box.fahm.fr", content = "10.123.128.5" }
    a_bunk = { type = "A", name = "bunk.fahm.fr", content = "10.123.185.3" }
    a_home = { type = "A", name = "home.fahm.fr", content = "203.0.113.10" }
    a_lab  = { type = "A", name = "lab.fahm.fr", content = "10.123.128.2" }
    a_mail = { type = "A", name = "mail.fahm.fr", content = "103.168.172.65", comment = "fastmail" }
    a_pug  = { type = "A", name = "pug.fahm.fr", content = "10.123.128.3" }

    # CNAME
    cname_auth                   = { type = "CNAME", name = "auth.fahm.fr", content = "lab.fahm.fr" }
    cname_click_noreply          = { type = "CNAME", name = "click.noreply.fahm.fr", content = "eu.mailgun.org", comment = "mailgun" }
    cname_fm1_domainkey          = { type = "CNAME", name = "fm1._domainkey.fahm.fr", content = "fm1.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    cname_fm2_domainkey          = { type = "CNAME", name = "fm2._domainkey.fahm.fr", content = "fm2.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    cname_fm3_domainkey          = { type = "CNAME", name = "fm3._domainkey.fahm.fr", content = "fm3.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    cname_pdk1_domainkey_noreply = { type = "CNAME", name = "pdk1._domainkey.noreply.fahm.fr", content = "pdk1._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    cname_pdk2_domainkey_noreply = { type = "CNAME", name = "pdk2._domainkey.noreply.fahm.fr", content = "pdk2._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    cname_wildcard_box           = { type = "CNAME", name = "*.box.fahm.fr", content = "box.fahm.fr" }
    cname_wildcard_bunk          = { type = "CNAME", name = "*.bunk.fahm.fr", content = "bunk.fahm.fr" }
    cname_wildcard_lab           = { type = "CNAME", name = "*.lab.fahm.fr", content = "lab.fahm.fr" }
    cname_wildcard_pug           = { type = "CNAME", name = "*.pug.fahm.fr", content = "pug.fahm.fr" }

    # TXT
    txt_dmarc         = { type = "TXT", name = "_dmarc.fahm.fr", content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:25d3ddf65216493ba512fa8d7568c3d7@dmarc-reports.cloudflare.net" }
    txt_dmarc_noreply = { type = "TXT", name = "_dmarc.noreply.fahm.fr", content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com; ruf=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com;", comment = "mailgun" }
    txt_spf_noreply   = { type = "TXT", name = "noreply.fahm.fr", content = "v=spf1 include:mailgun.org ~all", comment = "mailgun" }
    txt_spf           = { type = "TXT", name = "fahm.fr", content = "v=spf1 include:spf.messagingengine.com include:_spf.mx.cloudflare.net ~all" }

    # MX
    mx_fahm_fr_in1  = { type = "MX", name = "fahm.fr", content = "in1-smtp.messagingengine.com", priority = 10 }
    mx_fahm_fr_in2  = { type = "MX", name = "fahm.fr", content = "in2-smtp.messagingengine.com", priority = 20 }
    mx_wildcard_in1 = { type = "MX", name = "*.fahm.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    mx_wildcard_in2 = { type = "MX", name = "*.fahm.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    mx_noreply_mxa  = { type = "MX", name = "noreply.fahm.fr", content = "mxa.eu.mailgun.org", priority = 10, comment = "mailgun" }
    mx_noreply_mxb  = { type = "MX", name = "noreply.fahm.fr", content = "mxb.eu.mailgun.org", priority = 10, comment = "mailgun" }
  }
}

resource "cloudflare_dns_record" "fahm_fr" {
  for_each = var.fahm_fr_records

  zone_id  = local.zones["fahm.fr"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = each.value.priority
  proxied  = each.value.proxied
  ttl      = 1
  comment  = each.value.comment
  tags     = each.value.tags
}

# ---- SRV records (Fastmail service discovery) ----
# Separate from var.fahm_fr_records because SRV carries a nested
# data {} block rather than a flat content string. All SRV records in
# this zone are Fastmail; comment is hardcoded resource-wide.

locals {
  fahm_fr_srv_records = {
    "_autodiscover._tcp.fahm.fr" = { port = 443, priority = 0, target = "autodiscover.fastmail.com", weight = 1 }
    "_caldav._tcp.fahm.fr"       = { port = 0, priority = 0, target = ".", weight = 0 }
    "_caldavs._tcp.fahm.fr"      = { port = 443, priority = 0, target = "caldav.fastmail.com", weight = 1 }
    "_carddav._tcp.fahm.fr"      = { port = 0, priority = 0, target = ".", weight = 0 }
    "_carddavs._tcp.fahm.fr"     = { port = 443, priority = 0, target = "carddav.fastmail.com", weight = 1 }
    "_imap._tcp.fahm.fr"         = { port = 0, priority = 0, target = ".", weight = 0 }
    "_imaps._tcp.fahm.fr"        = { port = 993, priority = 0, target = "imap.fastmail.com", weight = 1 }
    "_jmap._tcp.fahm.fr"         = { port = 443, priority = 0, target = "api.fastmail.com", weight = 1 }
    "_pop3._tcp.fahm.fr"         = { port = 0, priority = 0, target = ".", weight = 0 }
    "_pop3s._tcp.fahm.fr"        = { port = 995, priority = 10, target = "pop.fastmail.com", weight = 1 }
    "_submission._tcp.fahm.fr"   = { port = 0, priority = 0, target = ".", weight = 0 }
    "_submissions._tcp.fahm.fr"  = { port = 465, priority = 0, target = "smtp.fastmail.com", weight = 1 }
  }
}

resource "cloudflare_dns_record" "fahm_fr_srv" {
  for_each = local.fahm_fr_srv_records

  zone_id  = local.zones["fahm.fr"]
  type     = "SRV"
  name     = each.key
  priority = each.value.priority
  proxied  = false
  ttl      = 1
  comment  = "fastmail"
  tags     = []

  data = {
    port     = each.value.port
    priority = each.value.priority
    target   = each.value.target
    weight   = each.value.weight
  }
}

# CF Email Routing's outbound DKIM key. Auto-provisioned and rotated
# server-side; tofu only tracks existence + presence here, not the key
# material. Let CF roll the key without flagging drift on every plan.
# Standalone because lifecycle blocks can't reference each.value.
resource "cloudflare_dns_record" "fahm_fr_txt_cf2024_1__domainkey" {
  content = "\"v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAiweykoi+o48IOGuP7GR3X0MOExCUDY/BCRHoWBnh3rChl7WhdyCxW3jgq1daEjPPqoi7sJvdg5hEQVsgVRQP4DcnQDVjGMbASQtrY4WmB1VebF+RPJB2ECPsEDTpeiI5ZyUAwJaVX7r6bznU67g7LvFq35yIo4sdlmtZGV+i0H4cpYH9+3JJ78k\" \"m4KXwaf9xUJCWF6nxeD+qG6Fyruw1Qlbds2r85U9dkNDVAS3gioCvELryh1TxKGiVTkg4wqHTyHfWsp7KD3WQHYJn0RyfJJu6YEmL77zonn7p2SRMvTMP3ZEXibnC9gz3nnhR6wcYL8Q7zXypKTMD58bTixDSJwIDAQAB\""
  name    = "cf2024-1._domainkey.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = local.zones["fahm.fr"]
  tags    = []

  lifecycle {
    ignore_changes = [content]
  }
}

# State migration from the previous "<TYPE>/<name>[/<content>]" key shape.
# Safe to prune once `tofu apply` confirms a no-op plan -- same pattern as
# commit 662463ca which pruned the dns.tf-era moves.
moved {
  from = cloudflare_dns_record.fahm_fr["A/box.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_box"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["A/bunk.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_bunk"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["A/home.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_home"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["A/lab.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_lab"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["A/mail.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_mail"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["A/pug.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["a_pug"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/auth.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_auth"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/click.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_click_noreply"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/fm1._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_fm1_domainkey"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/fm2._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_fm2_domainkey"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/fm3._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_fm3_domainkey"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/pdk1._domainkey.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_pdk1_domainkey_noreply"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/pdk2._domainkey.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_pdk2_domainkey_noreply"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/*.box.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_wildcard_box"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/*.bunk.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_wildcard_bunk"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/*.lab.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_wildcard_lab"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["CNAME/*.pug.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["cname_wildcard_pug"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["TXT/_dmarc.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["txt_dmarc"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["TXT/_dmarc.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["txt_dmarc_noreply"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["TXT/noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["txt_spf_noreply"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["TXT/fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["txt_spf"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/fahm.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["mx_fahm_fr_in1"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/fahm.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["mx_fahm_fr_in2"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/*.fahm.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["mx_wildcard_in1"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/*.fahm.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["mx_wildcard_in2"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/noreply.fahm.fr/mxa.eu.mailgun.org"]
  to   = cloudflare_dns_record.fahm_fr["mx_noreply_mxa"]
}
moved {
  from = cloudflare_dns_record.fahm_fr["MX/noreply.fahm.fr/mxb.eu.mailgun.org"]
  to   = cloudflare_dns_record.fahm_fr["mx_noreply_mxb"]
}

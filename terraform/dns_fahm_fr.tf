# DNS records for fahm.fr.
#
# Records (A/CNAME/TXT/MX) live in a single typed map; one
# cloudflare_dns_record resource iterates it. SRV records sit in their
# own map + resource because they carry a nested data {} block rather
# than a flat content string.
#
# Map key shape:
#   "<TYPE>/<name>"            for A/CNAME/TXT (one record per name+type)
#   "<TYPE>/<name>/<content>"  for MX (multiple records can share a name)
# Type + name are derived from the key by the resource block, so the
# map entries only carry the differing fields (content, priority,
# proxied, comment).
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

locals {
  fahm_fr_records = {
    # A
    "A/box.fahm.fr"  = { content = "10.123.128.5" }
    "A/bunk.fahm.fr" = { content = "10.123.185.3" }
    "A/home.fahm.fr" = { content = "203.0.113.10" }
    "A/lab.fahm.fr"  = { content = "10.123.128.2" }
    "A/mail.fahm.fr" = { content = "103.168.172.65", comment = "fastmail" }
    "A/pug.fahm.fr"  = { content = "10.123.128.3" }

    # CNAME
    "CNAME/auth.fahm.fr"                    = { content = "lab.fahm.fr" }
    "CNAME/click.noreply.fahm.fr"           = { content = "eu.mailgun.org", comment = "mailgun" }
    "CNAME/fm1._domainkey.fahm.fr"          = { content = "fm1.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "CNAME/fm2._domainkey.fahm.fr"          = { content = "fm2.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "CNAME/fm3._domainkey.fahm.fr"          = { content = "fm3.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "CNAME/pdk1._domainkey.noreply.fahm.fr" = { content = "pdk1._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    "CNAME/pdk2._domainkey.noreply.fahm.fr" = { content = "pdk2._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    "CNAME/*.box.fahm.fr"                   = { content = "box.fahm.fr" }
    "CNAME/*.bunk.fahm.fr"                  = { content = "bunk.fahm.fr" }
    "CNAME/*.lab.fahm.fr"                   = { content = "lab.fahm.fr" }
    "CNAME/*.pug.fahm.fr"                   = { content = "pug.fahm.fr" }

    # TXT
    "TXT/_dmarc.fahm.fr"         = { content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:25d3ddf65216493ba512fa8d7568c3d7@dmarc-reports.cloudflare.net" }
    "TXT/_dmarc.noreply.fahm.fr" = { content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com; ruf=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com;", comment = "mailgun" }
    "TXT/noreply.fahm.fr"        = { content = "v=spf1 include:mailgun.org ~all", comment = "mailgun" }
    "TXT/fahm.fr"                = { content = "v=spf1 include:spf.messagingengine.com include:_spf.mx.cloudflare.net ~all" }

    # MX (key = "MX/<name>/<content>" because multiple records share name)
    "MX/fahm.fr/in1-smtp.messagingengine.com"   = { content = "in1-smtp.messagingengine.com", priority = 10 }
    "MX/fahm.fr/in2-smtp.messagingengine.com"   = { content = "in2-smtp.messagingengine.com", priority = 20 }
    "MX/*.fahm.fr/in1-smtp.messagingengine.com" = { content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "MX/*.fahm.fr/in2-smtp.messagingengine.com" = { content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    "MX/noreply.fahm.fr/mxa.eu.mailgun.org"     = { content = "mxa.eu.mailgun.org", priority = 10, comment = "mailgun" }
    "MX/noreply.fahm.fr/mxb.eu.mailgun.org"     = { content = "mxb.eu.mailgun.org", priority = 10, comment = "mailgun" }
  }
}

resource "cloudflare_dns_record" "fahm_fr" {
  for_each = local.fahm_fr_records

  zone_id  = local.zones["fahm.fr"]
  type     = split("/", each.key)[0]
  name     = split("/", each.key)[1]
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
}

# ---- SRV records (Fastmail service discovery) ----
# Separate from local.fahm_fr_records because SRV carries a nested
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

  lifecycle {
    ignore_changes = [content]
  }
}

# ---- State migrations ----
# Map shared-resource for_each addresses to per-zone-unified addresses.
# Safe to delete after the next apply lands.

moved {
  from = cloudflare_dns_record.a["box.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/box.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.a["bunk.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/bunk.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.a["home.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/home.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.a["lab.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/lab.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.a["mail.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/mail.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.a["pug.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["A/pug.fahm.fr"]
}

moved {
  from = cloudflare_dns_record.cname["auth.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/auth.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["click.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/click.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["fm1._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/fm1._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["fm2._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/fm2._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["fm3._domainkey.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/fm3._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["pdk1._domainkey.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/pdk1._domainkey.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["pdk2._domainkey.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/pdk2._domainkey.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["*.box.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/*.box.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["*.bunk.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/*.bunk.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["*.lab.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/*.lab.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.cname["*.pug.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["CNAME/*.pug.fahm.fr"]
}

moved {
  from = cloudflare_dns_record.txt["_dmarc.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["TXT/_dmarc.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.txt["_dmarc.noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["TXT/_dmarc.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.txt["noreply.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["TXT/noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.txt["fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr["TXT/fahm.fr"]
}

moved {
  from = cloudflare_dns_record.mx["fahm.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["MX/fahm.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mx["fahm.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["MX/fahm.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mx["*.fahm.fr/in1-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["MX/*.fahm.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mx["*.fahm.fr/in2-smtp.messagingengine.com"]
  to   = cloudflare_dns_record.fahm_fr["MX/*.fahm.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mx["noreply.fahm.fr/mxa.eu.mailgun.org"]
  to   = cloudflare_dns_record.fahm_fr["MX/noreply.fahm.fr/mxa.eu.mailgun.org"]
}
moved {
  from = cloudflare_dns_record.mx["noreply.fahm.fr/mxb.eu.mailgun.org"]
  to   = cloudflare_dns_record.fahm_fr["MX/noreply.fahm.fr/mxb.eu.mailgun.org"]
}

moved {
  from = cloudflare_dns_record.srv["_autodiscover._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_autodiscover._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_caldav._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_caldav._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_caldavs._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_caldavs._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_carddav._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_carddav._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_carddavs._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_carddavs._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_imap._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_imap._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_imaps._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_imaps._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_jmap._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_jmap._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_pop3._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_pop3._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_pop3s._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_pop3s._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_submission._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_submission._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.srv["_submissions._tcp.fahm.fr"]
  to   = cloudflare_dns_record.fahm_fr_srv["_submissions._tcp.fahm.fr"]
}

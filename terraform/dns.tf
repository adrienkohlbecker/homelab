# DNS records for fahm.fr, mhaf.fr, adrienkohlbecker.com.
#
# Records are grouped by type into typed locals maps; one
# cloudflare_dns_record resource iterates each. Map keys are the
# human-readable record name; MX uses "<name>/<content>" because
# multiple records can share a name. Records with non-default
# proxied/comment carry those fields per map entry; everything else
# inherits the resource-level defaults (proxied=false, ttl=1).
#
# Adding a record: pick the matching local, add one map entry, plan,
# apply. No new resource block, no copy-pastable per-record boilerplate.
#
# Special case: the CF Email Routing outbound DKIM TXT
# (cf2024-1._domainkey.fahm.fr) needs lifecycle { ignore_changes = [content] }
# because CF auto-rotates the key server-side. It stays as a standalone
# resource at the bottom of this file -- lifecycle blocks can't reference
# each.value, so it can't live in the iterated map.

# ---- A records ----

locals {
  a_records = {
    "box.mhaf.fr"   = { zone = "mhaf.fr", content = "10.234.0.5" }
    "*.lab.mhaf.fr" = { zone = "mhaf.fr", content = "10.234.0.2" }
    "box.fahm.fr"   = { zone = "fahm.fr", content = "10.123.128.5" }
    "bunk.fahm.fr"  = { zone = "fahm.fr", content = "10.123.185.3" }
    "home.fahm.fr"  = { zone = "fahm.fr", content = "203.0.113.10" }
    "lab.fahm.fr"   = { zone = "fahm.fr", content = "10.123.128.2" }
    "mail.fahm.fr"  = { zone = "fahm.fr", content = "103.168.172.65", comment = "fastmail" }
    "pug.fahm.fr"   = { zone = "fahm.fr", content = "10.123.128.3" }
  }
}

resource "cloudflare_dns_record" "a" {
  for_each = local.a_records

  zone_id = local.zones[each.value.zone]
  type    = "A"
  name    = each.key
  content = each.value.content
  proxied = false
  ttl     = 1
  comment = try(each.value.comment, null)
}

# ---- CNAME records ----

locals {
  cname_records = {
    # mhaf.fr
    "*.box.mhaf.fr"          = { zone = "mhaf.fr", content = "box.mhaf.fr" }
    "echo.mhaf.fr"           = { zone = "mhaf.fr", content = "box.mhaf.fr", proxied = true }
    "fm1._domainkey.mhaf.fr" = { zone = "mhaf.fr", content = "fm1.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    "fm2._domainkey.mhaf.fr" = { zone = "mhaf.fr", content = "fm2.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    "fm3._domainkey.mhaf.fr" = { zone = "mhaf.fr", content = "fm3.mhaf.fr.dkim.fmhosted.com", comment = "fastmail" }
    # adrienkohlbecker.com (both proxied -- github-pages origin)
    "adrienkohlbecker.com"     = { zone = "adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    "www.adrienkohlbecker.com" = { zone = "adrienkohlbecker.com", content = "adrienkohlbecker.github.io", proxied = true }
    # fahm.fr
    "auth.fahm.fr"                    = { zone = "fahm.fr", content = "lab.fahm.fr" }
    "click.noreply.fahm.fr"           = { zone = "fahm.fr", content = "eu.mailgun.org", comment = "mailgun" }
    "fm1._domainkey.fahm.fr"          = { zone = "fahm.fr", content = "fm1.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "fm2._domainkey.fahm.fr"          = { zone = "fahm.fr", content = "fm2.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "fm3._domainkey.fahm.fr"          = { zone = "fahm.fr", content = "fm3.fahm.fr.dkim.fmhosted.com", comment = "fastmail" }
    "pdk1._domainkey.noreply.fahm.fr" = { zone = "fahm.fr", content = "pdk1._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    "pdk2._domainkey.noreply.fahm.fr" = { zone = "fahm.fr", content = "pdk2._domainkey.50bf8.dkim2.eu.mgsend.org", comment = "mailgun" }
    "*.box.fahm.fr"                   = { zone = "fahm.fr", content = "box.fahm.fr" }
    "*.bunk.fahm.fr"                  = { zone = "fahm.fr", content = "bunk.fahm.fr" }
    "*.lab.fahm.fr"                   = { zone = "fahm.fr", content = "lab.fahm.fr" }
    "*.pug.fahm.fr"                   = { zone = "fahm.fr", content = "pug.fahm.fr" }
  }
}

# echo.mhaf.fr is proxied so CF intercepts and applies the `echo` Access
# policies (see access.tf). The CNAME target is arbitrary -- CF gates the
# request before it reaches the origin; for this test fixture the origin
# doesn't need to respond.
resource "cloudflare_dns_record" "cname" {
  for_each = local.cname_records

  zone_id = local.zones[each.value.zone]
  type    = "CNAME"
  name    = each.key
  content = each.value.content
  proxied = try(each.value.proxied, false)
  ttl     = 1
  comment = try(each.value.comment, null)
}

# ---- TXT records ----

locals {
  txt_records = {
    "adrienkohlbecker.com"   = { zone = "adrienkohlbecker.com", content = "keybase-site-verification=ARwVSN_9cTudAafXA22PN2Iy7d17v6BHeEwjUPqth6M" }
    "_dmarc.fahm.fr"         = { zone = "fahm.fr", content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:25d3ddf65216493ba512fa8d7568c3d7@dmarc-reports.cloudflare.net" }
    "_dmarc.noreply.fahm.fr" = { zone = "fahm.fr", content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com; ruf=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com;", comment = "mailgun" }
    "noreply.fahm.fr"        = { zone = "fahm.fr", content = "v=spf1 include:mailgun.org ~all", comment = "mailgun" }
    "fahm.fr"                = { zone = "fahm.fr", content = "v=spf1 include:spf.messagingengine.com include:_spf.mx.cloudflare.net ~all" }
    "_dmarc.mhaf.fr"         = { zone = "mhaf.fr", content = "v=DMARC1; p=none;", comment = "fastmail" }
    "mhaf.fr"                = { zone = "mhaf.fr", content = "v=spf1 include:spf.messagingengine.com ?all", comment = "fastmail" }
  }
}

# DMARC posture is currently p=none across all three zones (observation
# only -- failing mail is delivered but flagged). Staged enforcement plan:
#
#   Stage 1 (now): p=none with rua=...; collect 2 weeks of aggregate
#     reports per zone. Targets are operator inboxes
#     (dmarc-reports.cloudflare.net for fahm.fr; mailgun + ondmarc for
#     noreply). Stay at this stage until the report stream shows only
#     legitimate senders -- Fastmail (in*-smtp.messagingengine.com) +
#     Mailgun (eu.mailgun.org) for fahm.fr + noreply.fahm.fr; Fastmail
#     only for mhaf.fr.
#
#   Stage 2 (after clean reports): p=quarantine; pct=25 for one week,
#     then pct=100. Tighten SPF to -all (hardfail) at the same time.
#     Verify no legitimate mail lands in spam from monitored receivers.
#
#   Stage 3 (production): p=reject. Forgeries with From: <user>@<zone>
#     get rejected by the receiver, eliminating the phishing-by-spoof
#     vector that p=none allows today.
#
# Homelab framing: spear-phishing the operator's contacts via spoofed
# fahm.fr / mhaf.fr From: headers is the abuse case; for the noreply
# Mailgun-sending zone the rollout is more clearly justified since
# every legitimate sender there is already DKIM-signing through pdk1/
# pdk2 selectors.
resource "cloudflare_dns_record" "txt" {
  for_each = local.txt_records

  zone_id = local.zones[each.value.zone]
  type    = "TXT"
  name    = each.key
  content = each.value.content
  proxied = false
  ttl     = 1
  comment = try(each.value.comment, null)
}

# CF Email Routing's outbound DKIM key. Auto-provisioned and rotated
# server-side; tofu only tracks existence + presence here, not the key
# material. Let CF roll the key without flagging drift on every plan.
# Standalone (not in local.txt_records) because lifecycle blocks can't
# reference each.value.
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

# ---- MX records ----
# Key shape: "<name>/<content>" because multiple records share a name
# (e.g. noreply.fahm.fr has both mxa and mxb).

locals {
  mx_records = {
    # Fastmail (mhaf.fr + fahm.fr, primary + wildcard)
    "fahm.fr/in1-smtp.messagingengine.com"   = { zone = "fahm.fr", name = "fahm.fr", content = "in1-smtp.messagingengine.com", priority = 10 }
    "fahm.fr/in2-smtp.messagingengine.com"   = { zone = "fahm.fr", name = "fahm.fr", content = "in2-smtp.messagingengine.com", priority = 20 }
    "*.fahm.fr/in1-smtp.messagingengine.com" = { zone = "fahm.fr", name = "*.fahm.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "*.fahm.fr/in2-smtp.messagingengine.com" = { zone = "fahm.fr", name = "*.fahm.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    "mhaf.fr/in1-smtp.messagingengine.com"   = { zone = "mhaf.fr", name = "mhaf.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "mhaf.fr/in2-smtp.messagingengine.com"   = { zone = "mhaf.fr", name = "mhaf.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    "*.mhaf.fr/in1-smtp.messagingengine.com" = { zone = "mhaf.fr", name = "*.mhaf.fr", content = "in1-smtp.messagingengine.com", priority = 10, comment = "fastmail" }
    "*.mhaf.fr/in2-smtp.messagingengine.com" = { zone = "mhaf.fr", name = "*.mhaf.fr", content = "in2-smtp.messagingengine.com", priority = 20, comment = "fastmail" }
    # Mailgun (noreply.fahm.fr)
    "noreply.fahm.fr/mxa.eu.mailgun.org" = { zone = "fahm.fr", name = "noreply.fahm.fr", content = "mxa.eu.mailgun.org", priority = 10, comment = "mailgun" }
    "noreply.fahm.fr/mxb.eu.mailgun.org" = { zone = "fahm.fr", name = "noreply.fahm.fr", content = "mxb.eu.mailgun.org", priority = 10, comment = "mailgun" }
  }
}

resource "cloudflare_dns_record" "mx" {
  for_each = local.mx_records

  zone_id  = local.zones[each.value.zone]
  type     = "MX"
  name     = each.value.name
  content  = each.value.content
  priority = each.value.priority
  proxied  = false
  ttl      = 1
  comment  = try(each.value.comment, null)
}

# ---- SRV records (Fastmail service discovery) ----
# All SRV records in this stack belong to Fastmail, so the comment is
# hardcoded resource-wide rather than per-entry.

locals {
  srv_records = {
    "_autodiscover._tcp.fahm.fr" = { zone = "fahm.fr", port = 443, priority = 0, target = "autodiscover.fastmail.com", weight = 1 }
    "_caldav._tcp.fahm.fr"       = { zone = "fahm.fr", port = 0, priority = 0, target = ".", weight = 0 }
    "_caldavs._tcp.fahm.fr"      = { zone = "fahm.fr", port = 443, priority = 0, target = "caldav.fastmail.com", weight = 1 }
    "_carddav._tcp.fahm.fr"      = { zone = "fahm.fr", port = 0, priority = 0, target = ".", weight = 0 }
    "_carddavs._tcp.fahm.fr"     = { zone = "fahm.fr", port = 443, priority = 0, target = "carddav.fastmail.com", weight = 1 }
    "_imap._tcp.fahm.fr"         = { zone = "fahm.fr", port = 0, priority = 0, target = ".", weight = 0 }
    "_imaps._tcp.fahm.fr"        = { zone = "fahm.fr", port = 993, priority = 0, target = "imap.fastmail.com", weight = 1 }
    "_jmap._tcp.fahm.fr"         = { zone = "fahm.fr", port = 443, priority = 0, target = "api.fastmail.com", weight = 1 }
    "_pop3._tcp.fahm.fr"         = { zone = "fahm.fr", port = 0, priority = 0, target = ".", weight = 0 }
    "_pop3s._tcp.fahm.fr"        = { zone = "fahm.fr", port = 995, priority = 10, target = "pop.fastmail.com", weight = 1 }
    "_submission._tcp.fahm.fr"   = { zone = "fahm.fr", port = 0, priority = 0, target = ".", weight = 0 }
    "_submissions._tcp.fahm.fr"  = { zone = "fahm.fr", port = 465, priority = 0, target = "smtp.fastmail.com", weight = 1 }
  }
}

resource "cloudflare_dns_record" "srv" {
  for_each = local.srv_records

  zone_id  = local.zones[each.value.zone]
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

# ---- State migrations ----
# Map pre-refactor per-record addresses to their new for_each-indexed
# addresses. Safe to delete after the next apply lands.

# A records
moved {
  from = cloudflare_dns_record.box_mhaf_fr
  to   = cloudflare_dns_record.a["box.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_a_wildcard_lab
  to   = cloudflare_dns_record.a["*.lab.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_box
  to   = cloudflare_dns_record.a["box.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_bunk
  to   = cloudflare_dns_record.a["bunk.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_home
  to   = cloudflare_dns_record.a["home.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_lab
  to   = cloudflare_dns_record.a["lab.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_mail
  to   = cloudflare_dns_record.a["mail.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_a_pug
  to   = cloudflare_dns_record.a["pug.fahm.fr"]
}

# CNAME records
moved {
  from = cloudflare_dns_record.star_box_mhaf_fr
  to   = cloudflare_dns_record.cname["*.box.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_cname_echo
  to   = cloudflare_dns_record.cname["echo.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_cname_fm1__domainkey
  to   = cloudflare_dns_record.cname["fm1._domainkey.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_cname_fm2__domainkey
  to   = cloudflare_dns_record.cname["fm2._domainkey.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_cname_fm3__domainkey
  to   = cloudflare_dns_record.cname["fm3._domainkey.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.adrienkohlbecker_com_cname_root
  to   = cloudflare_dns_record.cname["adrienkohlbecker.com"]
}
moved {
  from = cloudflare_dns_record.adrienkohlbecker_com_cname_www
  to   = cloudflare_dns_record.cname["www.adrienkohlbecker.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_auth
  to   = cloudflare_dns_record.cname["auth.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_click_noreply
  to   = cloudflare_dns_record.cname["click.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_fm1__domainkey
  to   = cloudflare_dns_record.cname["fm1._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_fm2__domainkey
  to   = cloudflare_dns_record.cname["fm2._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_fm3__domainkey
  to   = cloudflare_dns_record.cname["fm3._domainkey.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_pdk1__domainkey_noreply
  to   = cloudflare_dns_record.cname["pdk1._domainkey.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_pdk2__domainkey_noreply
  to   = cloudflare_dns_record.cname["pdk2._domainkey.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_wildcard_box
  to   = cloudflare_dns_record.cname["*.box.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_wildcard_bunk
  to   = cloudflare_dns_record.cname["*.bunk.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_wildcard_lab
  to   = cloudflare_dns_record.cname["*.lab.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_cname_wildcard_pug
  to   = cloudflare_dns_record.cname["*.pug.fahm.fr"]
}

# TXT records (cf2024-1 stays at its existing standalone address)
moved {
  from = cloudflare_dns_record.adrienkohlbecker_com_txt_root
  to   = cloudflare_dns_record.txt["adrienkohlbecker.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_txt_dmarc
  to   = cloudflare_dns_record.txt["_dmarc.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_txt_dmarc_noreply
  to   = cloudflare_dns_record.txt["_dmarc.noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_txt_noreply
  to   = cloudflare_dns_record.txt["noreply.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_txt_root
  to   = cloudflare_dns_record.txt["fahm.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_txt_dmarc
  to   = cloudflare_dns_record.txt["_dmarc.mhaf.fr"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_txt_root
  to   = cloudflare_dns_record.txt["mhaf.fr"]
}

# MX records
moved {
  from = cloudflare_dns_record.fahm_fr_mx_root__in1_smtp_messagingengine
  to   = cloudflare_dns_record.mx["fahm.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_mx_root__in2_smtp_messagingengine
  to   = cloudflare_dns_record.mx["fahm.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_mx_wildcard__in1_smtp_messagingengine
  to   = cloudflare_dns_record.mx["*.fahm.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_mx_wildcard__in2_smtp_messagingengine
  to   = cloudflare_dns_record.mx["*.fahm.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_mx_root__in1_smtp_messagingengine
  to   = cloudflare_dns_record.mx["mhaf.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_mx_root__in2_smtp_messagingengine
  to   = cloudflare_dns_record.mx["mhaf.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_mx_wildcard__in1_smtp_messagingengine
  to   = cloudflare_dns_record.mx["*.mhaf.fr/in1-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.mhaf_fr_mx_wildcard__in2_smtp_messagingengine
  to   = cloudflare_dns_record.mx["*.mhaf.fr/in2-smtp.messagingengine.com"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_mx_noreply__mxa_eu_mailgun_org
  to   = cloudflare_dns_record.mx["noreply.fahm.fr/mxa.eu.mailgun.org"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_mx_noreply__mxb_eu_mailgun_org
  to   = cloudflare_dns_record.mx["noreply.fahm.fr/mxb.eu.mailgun.org"]
}

# SRV records
moved {
  from = cloudflare_dns_record.fahm_fr_srv_autodiscover__tcp
  to   = cloudflare_dns_record.srv["_autodiscover._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_caldav__tcp
  to   = cloudflare_dns_record.srv["_caldav._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_caldavs__tcp
  to   = cloudflare_dns_record.srv["_caldavs._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_carddav__tcp
  to   = cloudflare_dns_record.srv["_carddav._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_carddavs__tcp
  to   = cloudflare_dns_record.srv["_carddavs._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_imap__tcp
  to   = cloudflare_dns_record.srv["_imap._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_imaps__tcp
  to   = cloudflare_dns_record.srv["_imaps._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_jmap__tcp
  to   = cloudflare_dns_record.srv["_jmap._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_pop3__tcp
  to   = cloudflare_dns_record.srv["_pop3._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_pop3s__tcp
  to   = cloudflare_dns_record.srv["_pop3s._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_submission__tcp
  to   = cloudflare_dns_record.srv["_submission._tcp.fahm.fr"]
}
moved {
  from = cloudflare_dns_record.fahm_fr_srv_submissions__tcp
  to   = cloudflare_dns_record.srv["_submissions._tcp.fahm.fr"]
}

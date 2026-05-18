# ---- mhaf.fr (existing managed) ----

resource "cloudflare_dns_record" "box_mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  type    = "A"
  name    = "box.mhaf.fr"
  content = "10.234.0.5"
  proxied = false
  ttl     = 1
}

resource "cloudflare_dns_record" "star_box_mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  type    = "CNAME"
  name    = "*.box.mhaf.fr"
  content = "box.mhaf.fr"
  proxied = false
  ttl     = 1
}

# ---- adrienkohlbecker.com ----

resource "cloudflare_dns_record" "adrienkohlbecker_com_cname_root" {
  content = "adrienkohlbecker.github.io"
  name    = "adrienkohlbecker.com"
  proxied = true
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
}

resource "cloudflare_dns_record" "adrienkohlbecker_com_cname_www" {
  content = "adrienkohlbecker.github.io"
  name    = "www.adrienkohlbecker.com"
  proxied = true
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
}

resource "cloudflare_dns_record" "adrienkohlbecker_com_txt_root" {
  content = "keybase-site-verification=ARwVSN_9cTudAafXA22PN2Iy7d17v6BHeEwjUPqth6M"
  name    = "adrienkohlbecker.com"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
}

# ---- fahm.fr ----

resource "cloudflare_dns_record" "fahm_fr_a_box" {
  content = "10.123.128.5"
  name    = "box.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_a_bunk" {
  content = "10.123.185.3"
  name    = "bunk.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_a_home" {
  content = "203.0.113.10"
  name    = "home.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_a_lab" {
  content = "10.123.128.2"
  name    = "lab.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_a_mail" {
  comment = "fastmail"
  content = "103.168.172.65"
  name    = "mail.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_a_pug" {
  content = "10.123.128.3"
  name    = "pug.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_auth" {
  content = "lab.fahm.fr"
  name    = "auth.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_click_noreply" {
  comment = "mailgun"
  content = "eu.mailgun.org"
  name    = "click.noreply.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_fm1__domainkey" {
  comment = "fastmail"
  content = "fm1.fahm.fr.dkim.fmhosted.com"
  name    = "fm1._domainkey.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_fm2__domainkey" {
  comment = "fastmail"
  content = "fm2.fahm.fr.dkim.fmhosted.com"
  name    = "fm2._domainkey.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_fm3__domainkey" {
  comment = "fastmail"
  content = "fm3.fahm.fr.dkim.fmhosted.com"
  name    = "fm3._domainkey.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_pdk1__domainkey_noreply" {
  comment = "mailgun"
  content = "pdk1._domainkey.50bf8.dkim2.eu.mgsend.org"
  name    = "pdk1._domainkey.noreply.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_pdk2__domainkey_noreply" {
  comment = "mailgun"
  content = "pdk2._domainkey.50bf8.dkim2.eu.mgsend.org"
  name    = "pdk2._domainkey.noreply.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_wildcard_box" {
  content = "box.fahm.fr"
  name    = "*.box.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_wildcard_bunk" {
  content = "bunk.fahm.fr"
  name    = "*.bunk.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_wildcard_lab" {
  content = "lab.fahm.fr"
  name    = "*.lab.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_cname_wildcard_pug" {
  content = "pug.fahm.fr"
  name    = "*.pug.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_noreply__mxa_eu_mailgun_org" {
  comment  = "mailgun"
  content  = "mxa.eu.mailgun.org"
  name     = "noreply.fahm.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_noreply__mxb_eu_mailgun_org" {
  comment  = "mailgun"
  content  = "mxb.eu.mailgun.org"
  name     = "noreply.fahm.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_root__in1_smtp_messagingengine" {
  content  = "in1-smtp.messagingengine.com"
  name     = "fahm.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_root__in2_smtp_messagingengine" {
  content  = "in2-smtp.messagingengine.com"
  name     = "fahm.fr"
  priority = 20
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_wildcard__in1_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in1-smtp.messagingengine.com"
  name     = "*.fahm.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_mx_wildcard__in2_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in2-smtp.messagingengine.com"
  name     = "*.fahm.fr"
  priority = 20
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_autodiscover__tcp" {
  comment = "fastmail"
  data = {
    port     = 443
    priority = 0
    target   = "autodiscover.fastmail.com"
    weight   = 1
  }
  name     = "_autodiscover._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_caldav__tcp" {
  comment = "fastmail"
  data = {
    port     = 0
    priority = 0
    target   = "."
    weight   = 0
  }
  name     = "_caldav._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_caldavs__tcp" {
  comment = "fastmail"
  data = {
    port     = 443
    priority = 0
    target   = "caldav.fastmail.com"
    weight   = 1
  }
  name     = "_caldavs._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_carddav__tcp" {
  comment = "fastmail"
  data = {
    port     = 0
    priority = 0
    target   = "."
    weight   = 0
  }
  name     = "_carddav._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_carddavs__tcp" {
  comment = "fastmail"
  data = {
    port     = 443
    priority = 0
    target   = "carddav.fastmail.com"
    weight   = 1
  }
  name     = "_carddavs._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_imap__tcp" {
  comment = "fastmail"
  data = {
    port     = 0
    priority = 0
    target   = "."
    weight   = 0
  }
  name     = "_imap._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_imaps__tcp" {
  comment = "fastmail"
  data = {
    port     = 993
    priority = 0
    target   = "imap.fastmail.com"
    weight   = 1
  }
  name     = "_imaps._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_jmap__tcp" {
  comment = "fastmail"
  data = {
    port     = 443
    priority = 0
    target   = "api.fastmail.com"
    weight   = 1
  }
  name     = "_jmap._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_pop3__tcp" {
  comment = "fastmail"
  data = {
    port     = 0
    priority = 0
    target   = "."
    weight   = 0
  }
  name     = "_pop3._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_pop3s__tcp" {
  comment = "fastmail"
  data = {
    port     = 995
    priority = 10
    target   = "pop.fastmail.com"
    weight   = 1
  }
  name     = "_pop3s._tcp.fahm.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_submission__tcp" {
  comment = "fastmail"
  data = {
    port     = 0
    priority = 0
    target   = "."
    weight   = 0
  }
  name     = "_submission._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_srv_submissions__tcp" {
  comment = "fastmail"
  data = {
    port     = 465
    priority = 0
    target   = "smtp.fastmail.com"
    weight   = 1
  }
  name     = "_submissions._tcp.fahm.fr"
  priority = 0
  proxied  = false
  ttl      = 1
  type     = "SRV"
  zone_id  = cloudflare_zone.fahm_fr.id
}

# CF Email Routing's outbound DKIM key. Auto-provisioned and rotated
# server-side; tofu only tracks existence + presence here, not the key
# material. Let CF roll the key without flagging drift on every plan.
resource "cloudflare_dns_record" "fahm_fr_txt_cf2024_1__domainkey" {
  content = "\"v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAiweykoi+o48IOGuP7GR3X0MOExCUDY/BCRHoWBnh3rChl7WhdyCxW3jgq1daEjPPqoi7sJvdg5hEQVsgVRQP4DcnQDVjGMbASQtrY4WmB1VebF+RPJB2ECPsEDTpeiI5ZyUAwJaVX7r6bznU67g7LvFq35yIo4sdlmtZGV+i0H4cpYH9+3JJ78k\" \"m4KXwaf9xUJCWF6nxeD+qG6Fyruw1Qlbds2r85U9dkNDVAS3gioCvELryh1TxKGiVTkg4wqHTyHfWsp7KD3WQHYJn0RyfJJu6YEmL77zonn7p2SRMvTMP3ZEXibnC9gz3nnhR6wcYL8Q7zXypKTMD58bTixDSJwIDAQAB\""
  name    = "cf2024-1._domainkey.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.fahm_fr.id

  lifecycle {
    ignore_changes = [content]
  }
}

# DMARC posture is currently p=none across all three zones (observation
# only -- failing mail is delivered but flagged). Staged enforcement plan
# (applies to _dmarc.fahm.fr below, _dmarc.noreply.fahm.fr, and
# _dmarc.mhaf.fr further down):
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
resource "cloudflare_dns_record" "fahm_fr_txt_dmarc" {
  content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:25d3ddf65216493ba512fa8d7568c3d7@dmarc-reports.cloudflare.net"
  name    = "_dmarc.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_txt_dmarc_noreply" {
  comment = "mailgun"
  content = "v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com; ruf=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com;"
  name    = "_dmarc.noreply.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_txt_noreply" {
  comment = "mailgun"
  content = "v=spf1 include:mailgun.org ~all"
  name    = "noreply.fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_dns_record" "fahm_fr_txt_root" {
  content = "v=spf1 include:spf.messagingengine.com include:_spf.mx.cloudflare.net ~all"
  name    = "fahm.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.fahm_fr.id
}

# ---- mhaf.fr ----

resource "cloudflare_dns_record" "mhaf_fr_cname_echo" {
  # Proxied so CF intercepts and applies the `echo` Access policies
  # (see access.tf). The CNAME target is arbitrary -- CF gates the
  # request before it reaches the origin; for this test fixture the
  # origin doesn't need to respond.
  content = "box.mhaf.fr"
  name    = "echo.mhaf.fr"
  proxied = true
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_a_wildcard_lab" {
  content = "10.234.0.2"
  name    = "*.lab.mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "A"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_cname_fm1__domainkey" {
  comment = "fastmail"
  content = "fm1.mhaf.fr.dkim.fmhosted.com"
  name    = "fm1._domainkey.mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_cname_fm2__domainkey" {
  comment = "fastmail"
  content = "fm2.mhaf.fr.dkim.fmhosted.com"
  name    = "fm2._domainkey.mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_cname_fm3__domainkey" {
  comment = "fastmail"
  content = "fm3.mhaf.fr.dkim.fmhosted.com"
  name    = "fm3._domainkey.mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "CNAME"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_mx_root__in1_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in1-smtp.messagingengine.com"
  name     = "mhaf.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_mx_root__in2_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in2-smtp.messagingengine.com"
  name     = "mhaf.fr"
  priority = 20
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_mx_wildcard__in1_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in1-smtp.messagingengine.com"
  name     = "*.mhaf.fr"
  priority = 10
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_mx_wildcard__in2_smtp_messagingengine" {
  comment  = "fastmail"
  content  = "in2-smtp.messagingengine.com"
  name     = "*.mhaf.fr"
  priority = 20
  proxied  = false
  ttl      = 1
  type     = "MX"
  zone_id  = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_txt_dmarc" {
  comment = "fastmail"
  content = "v=DMARC1; p=none;"
  name    = "_dmarc.mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.mhaf_fr.id
}

resource "cloudflare_dns_record" "mhaf_fr_txt_root" {
  comment = "fastmail"
  content = "v=spf1 include:spf.messagingengine.com ?all"
  name    = "mhaf.fr"
  proxied = false
  ttl     = 1
  type    = "TXT"
  zone_id = cloudflare_zone.mhaf_fr.id
}

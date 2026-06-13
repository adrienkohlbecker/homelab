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
# DMARC posture: fahm.fr + noreply.fahm.fr at p=quarantine; pct=25.
# mhaf.fr is on a separate (slower) track and stays at p=none.
# Aggregate reports flow to dmarc-reports.cloudflare.net (fahm.fr)
# and mailgun+ondmarc (noreply.fahm.fr).

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
    # A — host records (box/bunk/lab/pug) derive from
    # data/network_topology.yml in `local.fahm_fr_host_records` below; the
    # home WAN A record reads var.home_wan_ip via local.fahm_fr_wan_records.
    # This default holds only the static records that reference neither the
    # topology nor a variable (the Fastmail relay).
    a_mail = { type = "A", name = "mail.fahm.fr", content = "103.168.172.65", comment = "fastmail" }

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
    cname_wildcard_fox           = { type = "CNAME", name = "*.fox.fahm.fr", content = "fox.fahm.fr" }
    cname_wildcard_lab           = { type = "CNAME", name = "*.lab.fahm.fr", content = "lab.fahm.fr" }
    cname_wildcard_pug           = { type = "CNAME", name = "*.pug.fahm.fr", content = "pug.fahm.fr" }

    # TXT
    txt_dmarc         = { type = "TXT", name = "_dmarc.fahm.fr", content = "v=DMARC1; p=quarantine; pct=25; fo=1; ri=3600; rua=mailto:25d3ddf65216493ba512fa8d7568c3d7@dmarc-reports.cloudflare.net" }
    txt_dmarc_noreply = { type = "TXT", name = "_dmarc.noreply.fahm.fr", content = "v=DMARC1; p=quarantine; pct=25; fo=1; ri=3600; rua=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com; ruf=mailto:131310e2@dmarc.mailgun.org,mailto:37b6e2d1@inbox.ondmarc.com;", comment = "mailgun" }
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

locals {
  # Host A records — derived from data/network_topology.yml. lab/pug/bunk resolve
  # to their Tailscale CGNAT IPs (100.64.0.x) so that tailnet clients reach them
  # peer-to-peer without needing "Use Tailscale subnets" / --accept-routes. The
  # AdGuard split-horizon (roles/adguard) overrides these for clients on the home
  # estate, which reach the host over a more direct path. Non-tailnet internet
  # traffic to *.fahm.fr always gets an unroutable CGNAT address — the intended
  # fail-closed posture. box has no stable Tailscale IP (test fixture,
  # infrequently enrolled) so it keeps its physical address.
  #
  # fox is here too (Tailscale IP, A-only) with no AdGuard override: it is
  # off-home (Hetzner), so the estate reaches it over the mesh rather than a
  # LAN/wg path, same as a roaming client. fox's public IPs now serve only the
  # headscale.fahm.fr control plane (fahm_fr_headscale_records below).
  fahm_fr_host_records = {
    a_box  = { type = "A", name = "box.fahm.fr", content = local.network.hosts.box.physical }
    a_bunk = { type = "A", name = "bunk.fahm.fr", content = local.network.hosts.bunk.tailscale }
    a_fox  = { type = "A", name = "fox.fahm.fr", content = local.network.hosts.fox.tailscale }
    a_lab  = { type = "A", name = "lab.fahm.fr", content = local.network.hosts.lab.tailscale }
    a_pug  = { type = "A", name = "pug.fahm.fr", content = local.network.hosts.pug.tailscale }
  }

  # headscale.fahm.fr is fox's control-plane + DERP-relay FQDN, pinned to the
  # reserved Hetzner primary IPs -- the one intentional exception to the
  # fail-closed CGNAT posture (reachable from the internet so an un-enrolled
  # client can reach the control plane to join). It is decoupled from fox.fahm.fr
  # (now the Tailscale CGNAT IP, in fahm_fr_host_records) so the *.fox.fahm.fr
  # CNAME chasing fox.fahm.fr can't drag the control plane onto that
  # unreachable address. Dual-stack: the AAAA lets IPv6-only clients reach the
  # relay without NAT64 (nginx [::]:443 on fox, DERP STUN on [::]:3478). This
  # local bypasses the var.fahm_fr_records A/CNAME/TXT/MX validation, so the AAAA
  # needs no schema change there.
  fahm_fr_headscale_records = {
    a_headscale    = { type = "A", name = "headscale.fahm.fr", content = hcloud_primary_ip.fox.ip_address }
    aaaa_headscale = { type = "AAAA", name = "headscale.fahm.fr", content = hcloud_primary_ip.fox_v6.ip_address }
  }

  # home.fahm.fr — residential WAN IP from var.home_wan_ip (TF_VAR via
  # 1Password), kept out of this public repo. In a local rather than
  # var.fahm_fr_records' default because variable defaults can't reference
  # another variable.
  fahm_fr_wan_records = {
    a_home = { type = "A", name = "home.fahm.fr", content = var.home_wan_ip }
  }
  fahm_fr_records = merge(var.fahm_fr_records, local.fahm_fr_host_records, local.fahm_fr_headscale_records, local.fahm_fr_wan_records)
}

resource "cloudflare_dns_record" "fahm_fr" {
  for_each = local.fahm_fr_records

  # `try()` lets each.value be either a fully-typed var entry
  # (with optional defaults filled in) or a minimal local entry
  # (just type/name/content). Necessary because the variable's
  # typed-object schema doesn't apply to records merged in from
  # local.fahm_fr_host_records.
  zone_id  = local.zones["fahm.fr"]
  type     = each.value.type
  name     = each.value.name
  content  = each.value.content
  priority = try(each.value.priority, null)
  proxied  = try(each.value.proxied, false)
  ttl      = 1
  comment  = try(each.value.comment, null)
  tags     = try(each.value.tags, [])
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

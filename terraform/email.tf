# Email Routing chain: fahm.fr's catchall delivers every inbound
# message to the catchall-email worker (see workers.tf), which routes
# by recipient prefix to one of the verified addresses below. The
# adrienkohlbecker.com and mhaf.fr zones have routing disabled with
# `drop` catchalls -- they exist so future state changes show up as
# diffs instead of unmanaged side-channels.

# --- destination addresses (account-scoped) ---

resource "cloudflare_email_routing_address" "adrien_gmail" {
  account_id = local.cloudflare_account_id
  email      = "adrien.kohlbecker@gmail.com"
}

resource "cloudflare_email_routing_address" "spouse_email" {
  account_id = local.cloudflare_account_id
  email      = "spouse@example.com"
}

# --- per-zone settings (enabled toggle) ---

resource "cloudflare_email_routing_settings" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
}

resource "cloudflare_email_routing_settings" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
}

resource "cloudflare_email_routing_settings" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
}

# --- catch-all rules (one per zone; CF auto-creates these) ---

resource "cloudflare_email_routing_catch_all" "adrienkohlbecker_com" {
  zone_id = cloudflare_zone.adrienkohlbecker_com.id
  matchers = [{
    type = "all"
  }]
  actions = [{
    type = "drop"
  }]
  enabled = false
  name    = ""
}

resource "cloudflare_email_routing_catch_all" "fahm_fr" {
  zone_id = cloudflare_zone.fahm_fr.id
  matchers = [{
    type = "all"
  }]
  actions = [{
    type  = "worker"
    value = ["catchall-email"]
  }]
  enabled = true
  name    = ""
}

resource "cloudflare_email_routing_catch_all" "mhaf_fr" {
  zone_id = cloudflare_zone.mhaf_fr.id
  matchers = [{
    type = "all"
  }]
  actions = [{
    type = "drop"
  }]
  enabled = false
  name    = ""
}

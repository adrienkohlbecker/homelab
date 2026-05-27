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

# Resolved from TF_VAR_spouse_email in mise.toml [env] (1Password via op
# run) — keeps the address out of this public repo.
variable "spouse_email" {
  type      = string
  sensitive = true

  validation {
    condition     = can(regex("@", var.spouse_email))
    error_message = "spouse_email must be an email address (resolved via TF_VAR_spouse_email from 1Password)."
  }
}

resource "cloudflare_email_routing_address" "spouse_email" {
  account_id = local.cloudflare_account_id
  email      = var.spouse_email
}

# --- per-zone settings (enabled toggle) ---

resource "cloudflare_email_routing_settings" "this" {
  for_each = local.zones
  zone_id  = each.value
}

# --- catch-all rules (one per zone; CF auto-creates these) ---

locals {
  # action_type + action_value as flat fields (not a nested object)
  # because terraform's type inference across the map collapses any
  # heterogeneity (worker carries a value list; drop doesn't) and the
  # provider's required-check on actions[0].type then sees a dynamic
  # type instead of "worker" / "drop". Flat fields with uniform types
  # (string + list(string)) keep inference clean.
  email_routing_catchalls = {
    "fahm.fr" = {
      enabled      = true
      action_type  = "worker"
      action_value = [cloudflare_workers_script.catchall_email.script_name]
    }
    "mhaf.fr" = {
      enabled      = false
      action_type  = "drop"
      action_value = []
    }
    "adrienkohlbecker.com" = {
      enabled      = false
      action_type  = "drop"
      action_value = []
    }
  }
}

resource "cloudflare_email_routing_catch_all" "this" {
  for_each = local.email_routing_catchalls

  zone_id  = local.zones[each.key]
  matchers = [{ type = "all" }]
  enabled  = each.value.enabled
  name     = ""
  actions  = [{ type = each.value.action_type, value = each.value.action_value }]
}

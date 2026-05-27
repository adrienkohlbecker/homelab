locals {
  # Route map: local-part-prefix -> destination addresses. The worker
  # parses recipient as `<prefix>[.<suffix>]@<domain>`, looks up <prefix>
  # in this map (case-insensitively), and forwards to each address.
  # Multi-destination aliases (cp.* -> both) live here, not in the JS.
  # Destination addresses come from the cloudflare_email_routing_address
  # resources in email.tf so the worker auto-tracks renames there.
  email_routes = {
    ak = [cloudflare_email_routing_address.adrien_gmail.email]
    # spouse prefix sourced from 1P (var.spouse_initials) so the initials
    # aren't in this public repo; the value is unchanged so routing is intact.
    (var.spouse_initials) = [cloudflare_email_routing_address.spouse_email.email]
    # couple alias prefix, also sourced from 1P (var.couple_alias); value unchanged.
    (var.couple_alias) = [cloudflare_email_routing_address.adrien_gmail.email, cloudflare_email_routing_address.spouse_email.email]
  }
}

# Spouse's email-route prefix (local-part). Resolved from TF_VAR_spouse_initials
# in mise.toml [env] (1Password via op run) -- keeps the initials out of this
# public repo. Value unchanged, so the route map is identical at apply time.
variable "spouse_initials" {
  type      = string
  sensitive = true

  validation {
    condition     = length(var.spouse_initials) > 0
    error_message = "spouse_initials must be non-empty (resolved via TF_VAR_spouse_initials from 1Password)."
  }
}

# Couple email-route prefix (the adrien+spouse combined alias). Resolved from
# TF_VAR_couple_alias in mise.toml [env] (1Password) -- value unchanged.
variable "couple_alias" {
  type      = string
  sensitive = true

  validation {
    condition     = length(var.couple_alias) > 0
    error_message = "couple_alias must be non-empty (resolved via TF_VAR_couple_alias from 1Password)."
  }
}

resource "cloudflare_workers_script" "catchall_email" {
  account_id  = local.cloudflare_account_id
  script_name = "catchall-email"
  content     = file("${path.module}/workers/catchall_email.js")
  # main_module flags the upload as ES-module-syntax (the JS uses
  # `export default {...}`). Without it the provider sends as a
  # classic service worker and CF rejects with 10021 / "Unexpected
  # token 'export'". The value is just a filename label; CF uses it
  # to identify the entry point within the upload bundle.
  main_module = "catchall_email.js"
  # Pinned to upload date (2024-08-02). Bump deliberately when changing
  # script behaviour, not casually -- new compat dates can flip JS
  # runtime semantics under the worker.
  compatibility_date = "2024-08-02"
  usage_model        = "standard"

  bindings = [{
    type = "json"
    name = "ROUTES"
    json = jsonencode(local.email_routes)
  }]
}

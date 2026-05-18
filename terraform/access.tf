# Zero Trust Access surface. Kept around as a fixture for testing the
# traefik-auth-cloudflare repo against a real CF Access endpoint
# (echo.mhaf.fr), not as production auth -- nothing in this repo
# depends on it. Originally hosted at echo.mhaf.fr; migrated to
# mhaf.fr in 2026-05 because kohlby.fr was no longer in this account
# and CF's app-PUT validation rejected any update.

# Cookie / session hardening: http_only blocks JS access to the
# CF_Authorization cookie (XSS-via-echo can't exfil the token);
# enable_binding_cookie pins the session to the originating IP so a
# leaked token isn't portable; allowed_idps bind the app to Google
# explicitly rather than implicitly accepting "any verified IdP"; 24h
# session caps blast radius on a test fixture. Override per-policy
# below if longer windows are needed for a specific test path.
resource "cloudflare_zero_trust_access_application" "echo" {
  account_id                 = data.cloudflare_account.main.account_id
  name                       = "echo"
  type                       = "self_hosted"
  domain                     = "echo.mhaf.fr"
  session_duration           = "24h"
  app_launcher_visible       = true
  enable_binding_cookie      = true
  http_only_cookie_attribute = true
  auto_redirect_to_identity  = false
  options_preflight_bypass   = false

  allowed_idps = [cloudflare_zero_trust_access_identity_provider.google.id]

  destinations = [{
    type = "public"
    uri  = "echo.mhaf.fr"
  }]

  policies = [
    { id = cloudflare_zero_trust_access_policy.echo_me.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.echo_token.id, precedence = 2 },
  ]
}

# Reusable policies (replacing the original inline ones, which the v5
# provider can't manage in-place). Once the apply lands and the echo
# app re-points to these, the original inline policies become
# orphaned -- delete them via direct API call after apply.
resource "cloudflare_zero_trust_access_policy" "echo_me" {
  account_id       = data.cloudflare_account.main.account_id
  name             = "me"
  decision         = "allow"
  session_duration = "24h"

  include = [{
    email = { email = "adrien.kohlbecker@gmail.com" }
  }]
}

resource "cloudflare_zero_trust_access_policy" "echo_token" {
  account_id       = data.cloudflare_account.main.account_id
  name             = "token"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    any_valid_service_token = {}
  }]
}

variable "google_idp_client_id" {
  type        = string
  description = "Google OAuth client ID for the Access IdP."
}

variable "google_idp_client_secret" {
  type        = string
  sensitive   = true
  description = "Google OAuth client secret for the Access IdP. Sourced via TF_VAR_google_idp_client_secret in mise.toml (op:// reference)."
}

resource "cloudflare_zero_trust_access_identity_provider" "google" {
  account_id = data.cloudflare_account.main.account_id
  name       = "Google"
  type       = "google"

  config = {
    client_id     = var.google_idp_client_id
    client_secret = var.google_idp_client_secret
  }

  # scim_config omitted entirely -- SCIM isn't in use here. Provider
  # treats this as `null` and CF leaves the disabled config in place.
}

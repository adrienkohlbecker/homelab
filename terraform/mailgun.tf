# Mailgun powers transactional / notification email for the homelab: four
# service SMTP credentials (postfix/sabnzbd/gitea/overseerr@
# noreply.fahm.fr) relay through smtp.eu.mailgun.org. The DNS surface lives in
# dns_fahm_fr.tf; this file owns the Mailgun-side domain configuration those
# records point at.
#
# Auth uses an account-scoped Mailgun Private API key (settings ->
# "API security" in the Mailgun UI). Stored in 1Password and surfaced
# via TF_VAR_mailgun_api_key in mise.toml [env].
#
# Surfaces intentionally NOT under tofu:
#
# - mailgun_domain_credential (the 4 SMTP creds). Passwords live in
#   group_vars/prod.yml as ansible-vault entries and are consumed
#   directly by service roles. Bringing them under tofu would require
#   a terraform-output -> ansible-vault sync mechanism (vault is
#   already the canonical store on the ansible side). Revisit if
#   we ever centralize secret storage; until then, vault is fine.
#
# - mailgun_route. Inbound mail uses Cloudflare Email Routing (see
#   email.tf), not Mailgun. No routes configured.
#
# - mailgun_webhook. No event consumers wired up today.

variable "mailgun_api_key" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.mailgun_api_key) > 0
    error_message = "mailgun_api_key must be non-empty (resolved via TF_VAR_mailgun_api_key from 1Password through `op run`)."
  }
}

provider "mailgun" {
  api_key = var.mailgun_api_key
}

# EU region: domain hosted on eu.mailgun.org (matched by the pdk1/pdk2
# DKIM CNAMEs in dns_fahm_fr.tf and the .dkim2.eu.mgsend.org. targets,
# which are Mailgun's automatic-sender-security pattern --
# use_automatic_sender_security stays on so tofu doesn't fight the UI).
#
# smtp_password is the postmaster credential issued at domain creation;
# Mailgun doesn't return it on subsequent reads, so leaving it unset in
# HCL is the right shape. Per-service SMTP creds are separate
# mailgun_domain_credential objects (see header for why those aren't
# managed here).
resource "mailgun_domain" "noreply_fahm_fr" {
  name                          = "noreply.fahm.fr"
  region                        = "eu"
  spam_action                   = "disabled"
  wildcard                      = false
  open_tracking                 = false
  click_tracking                = false
  web_scheme                    = "http"
  use_automatic_sender_security = true
}

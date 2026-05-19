# Mailgun powers transactional / notification email for the homelab:
# the CI workflows (mise run ci:mail-cross-cut / ci:mail-failures) post
# through Mailgun's HTTP API, and five service SMTP credentials
# (postfix/sabnzbd/gitea/healthchecks/overseerr@noreply.fahm.fr) relay
# through smtp.eu.mailgun.org. The DNS surface (MX, DKIM, SPF, DMARC,
# click tracking CNAME) is in dns.tf; this file owns the Mailgun-side
# domain configuration that those DNS records point at.
#
# Auth uses an account-scoped Mailgun Private API key (settings ->
# "API security" in the Mailgun UI). Stored in 1Password and surfaced
# via TF_VAR_mailgun_api_key in mise.toml [env].
#
# Surfaces intentionally NOT under tofu:
#
# - mailgun_domain_credential (the 5 SMTP creds). Passwords live in
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

# Dedicated CI sending key, pushed into the homelab repo's
# MAILGUN_API_KEY GitHub Actions secret (see github.tf). Scoped to
# role=sending + this domain so a CI compromise can't manage domains,
# webhooks, or other credentials -- it can only send mail through
# noreply.fahm.fr. The master account API key in var.mailgun_api_key
# stays on the operator's workstation (mise.toml + 1P) and is never
# exposed to CI.
#
# Rotate with `tofu apply -replace=mailgun_api_key.ci_send_noreply`,
# which mints a new key, retires the old one, and pushes the new
# secret value into GitHub in the same apply.
resource "mailgun_api_key" "ci_send_noreply" {
  region      = "eu"
  role        = "sending"
  kind        = "domain"
  domain_name = mailgun_domain.noreply_fahm_fr.name
  description = "homelab CI (.github/workflows/test*.yml mail steps)"

  # NB: the wgebis/mailgun provider's Read returns secret=null on every
  # refresh (Mailgun's API only returns the secret in the Create
  # response). The consuming github_actions_secret.mailgun_api_key
  # (github.tf) carries the ignore_changes + replace_triggered_by
  # workaround so a `tofu apply` after refresh doesn't push null to
  # the CI secret. Rotation via -replace on this resource is the
  # supported path.
}

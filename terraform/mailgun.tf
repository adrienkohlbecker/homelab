# Mailgun relays transactional email for homelab services through
# smtp.eu.mailgun.org. This file owns the Mailgun-side noreply.fahm.fr domain;
# dns_fahm_fr.tf owns its DNS records.
#
# Auth uses an account-scoped Mailgun Private API key (settings ->
# "API security" in the Mailgun UI). Stored in 1Password and surfaced
# via TF_VAR_mailgun_api_key in mise.toml [env].
#
# SMTP credential passwords live in group_vars/prod.yml as Ansible Vault
# entries and are consumed directly by service roles. No inbound routes or
# event webhooks are managed here.

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
# Mailgun doesn't return it on subsequent reads, so leaving it unset in HCL is
# the right shape. Service-specific SMTP credentials remain Ansible-owned.
resource "mailgun_domain" "noreply_fahm_fr" {
  name                          = "noreply.fahm.fr"
  region                        = "eu"
  use_automatic_sender_security = true
}

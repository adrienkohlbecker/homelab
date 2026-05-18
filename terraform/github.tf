# GitHub Actions repo secrets that this repo's workflows consume. Only
# the secrets whose canonical source is somewhere terraform can read are
# managed here -- the vault-encrypted HOMELAB_VAULT_PASSWORD_TEST stays
# on the manual `gh secret set` path documented in CLAUDE.md until it's
# migrated.
#
# Auth: integrations/github v6 falls back to `gh auth token` when neither
# `token =` nor $GITHUB_TOKEN is set. Same pattern roles/github_runner uses
# to fetch the runner registration token (gh on the operator's machine,
# authenticated via keyring) -- nothing new to provision. If `gh auth login`
# hasn't run, plan fails at provider configure time.
# This token is *only* for terraform's repo-management calls (managing
# secrets); CI secrets that need an authenticated GitHub identity are
# dedicated PATs, see MISE_GITHUB_TOKEN below.

provider "github" {
  owner = "adrienkohlbecker"
}

# NEXUS_USERNAME / NEXUS_PASSWORD secrets pushed into each GitHub repo
# that has a matching nexus hosted-docker repo. Each repo's build
# workflow uses its own scoped credential against nexus.lab.fahm.fr;
# a password rotation (`tofu apply -replace='random_password.
# nexus_push["<name>"]'`) updates the user, the matching github secret,
# and any subsequent CI run in one apply. Assumes the GitHub repo name
# matches the nexus hosted-repo name (true today for homelab + compta).
resource "github_actions_secret" "nexus_username" {
  for_each = nexus_security_user.push

  repository  = each.key
  secret_name = "NEXUS_USERNAME"
  value       = each.value.userid
}

resource "github_actions_secret" "nexus_password" {
  for_each = nexus_security_user.push

  repository  = each.key
  secret_name = "NEXUS_PASSWORD"
  value       = random_password.nexus_push[each.key].result

  lifecycle {
    replace_triggered_by = [random_password.nexus_push[each.key]]
  }
}

moved {
  from = github_actions_secret.nexus_username
  to   = github_actions_secret.nexus_username["homelab"]
}

moved {
  from = github_actions_secret.nexus_password
  to   = github_actions_secret.nexus_password["homelab"]
}

# MISE_GITHUB_TOKEN raises mise's anonymous 60/hr GitHub API rate limit
# during `mise install` in the ci-image workflow. mise just
# needs an authenticated token; the value here is a dedicated
# fine-grained PAT minted in the GitHub UI with *no* scopes (public
# read is implicit) so a CI compromise can't pivot. Sourced from
# 1Password via the TF_VAR below -- create the 1P item once
# (item: "github-mise-token", field "credential"), then rotate by
# generating a new PAT, updating the 1P field, and re-applying.
variable "mise_github_token" {
  type      = string
  sensitive = true
}

resource "github_actions_secret" "mise_github_token" {
  repository  = "homelab"
  secret_name = "MISE_GITHUB_TOKEN"
  value       = var.mise_github_token
}

# MAILGUN_API_KEY + MAILGUN_DOMAIN consumed by .github/workflows/test*.yml
# (cross-cut and failure-notification mail steps via Mailgun's HTTP API,
# see CLAUDE.md "Continuous Integration"). The pushed key is the
# domain-scoped, role=sending key minted in mailgun.tf; the master
# account API key (var.mailgun_api_key) is never exposed to CI.
resource "github_actions_secret" "mailgun_api_key" {
  repository  = "homelab"
  secret_name = "MAILGUN_API_KEY"
  value       = mailgun_api_key.ci_send_noreply.secret

  lifecycle {
    # mailgun_api_key.ci_send_noreply.secret refreshes to null on every
    # refresh (wgebis/mailgun provider bug -- Read can't recover the
    # secret post-Create). Without ignore_changes here, every apply
    # would push null to the GitHub MAILGUN_API_KEY secret and break
    # CI mail. Rotation still works: `tofu apply -replace=mailgun_
    # api_key.ci_send_noreply` mints a new secret in the Create
    # response, replace_triggered_by below forces this resource to be
    # re-created with the new value, the GitHub secret updates.
    ignore_changes       = [value]
    replace_triggered_by = [mailgun_api_key.ci_send_noreply]
  }
}

resource "github_actions_secret" "mailgun_domain" {
  repository  = "homelab"
  secret_name = "MAILGUN_DOMAIN"
  value       = mailgun_domain.noreply_fahm_fr.name
}

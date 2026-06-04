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

# Scoped push credential for the zbm raw hosted repo (nexus.tf), consumed by the
# zbm-build workflow to PUT the validation tarball. Lands in the homelab repo
# where zbm-build runs; rotation mirrors the docker creds (-replace the
# random_password).
resource "github_actions_secret" "nexus_zbm_username" {
  repository  = "homelab"
  secret_name = "NEXUS_ZBM_USERNAME"
  value       = nexus_security_user.zbm_push.userid
}

resource "github_actions_secret" "nexus_zbm_password" {
  repository  = "homelab"
  secret_name = "NEXUS_ZBM_PASSWORD"
  value       = random_password.nexus_zbm_push.result

  lifecycle {
    replace_triggered_by = [random_password.nexus_zbm_push]
  }
}

# MISE_GITHUB_TOKEN raises mise's anonymous 60/hr GitHub API rate limit
# during `mise install` in the ci-image workflow. mise just
# needs an authenticated token; the value here is a dedicated
# fine-grained PAT minted in the GitHub UI with *no* scopes (public
# read is implicit) so a CI compromise can't pivot. Sourced from
# 1Password via the TF_VAR below -- create the 1P item once
# (item: "github-mise-token", field "credential"), then rotate by
# generating a new PAT, updating the 1P field, and re-applying.
# Not ephemeral: integrations/github v6.12.1's github_actions_secret.value
# is not a write-only attribute, so this token has to land in state to be
# referenced by the resource. Revisit when the provider grows write-only
# support (see github_actions_secret docs note "this does not hide it from
# state files").
variable "mise_github_token" {
  type      = string
  sensitive = true

  validation {
    condition     = length(var.mise_github_token) > 0
    error_message = "mise_github_token must be non-empty (resolved via TF_VAR_mise_github_token from 1Password through `op run`)."
  }
}

resource "github_actions_secret" "mise_github_token" {
  repository  = "homelab"
  secret_name = "MISE_GITHUB_TOKEN"
  value       = var.mise_github_token
}

# HCLOUD_TOKEN (consumed by the packer-build and ci workflows for hetzner
# snapshot builds) is deliberately NOT managed here: integrations/github 6.x has
# no write-only value attribute, so a terraform-managed secret lands its
# plaintext in state (the provider docs say so outright). It's a full-project
# Hetzner token, so we keep it off state entirely via the manual `gh secret set`
# path -- same treatment as HOMELAB_VAULT_PASSWORD_TEST. Runbook:
# notes/github-actions-ci.md, phase 3.

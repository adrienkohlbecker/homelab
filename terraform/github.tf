# GitHub Actions CI is retired (replaced by GitLab CI). The repo-secret
# resources that fed the old workflows -- NEXUS_USERNAME / NEXUS_PASSWORD (one
# pair per nexus push repo) and MISE_GITHUB_TOKEN -- are removed from config.
# This provider block is kept transiently so `tofu apply` can DESTROY those
# github_actions_secret resources still in state. Once that destroy apply has
# landed, delete this file and the `github` entry in main.tf's required_providers.
#
# Auth: integrations/github v6 falls back to `gh auth token` when neither
# `token =` nor $GITHUB_TOKEN is set -- nothing to provision beyond an
# authenticated `gh` on the operator's machine.
provider "github" {
  owner = "adrienkohlbecker"
}

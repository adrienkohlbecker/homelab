#!/usr/bin/env bash
#MISE description="Validate .gitlab-ci.yml against the GitLab CI lint API"
set -euo pipefail

# Full pipeline validation (include: expansion, job graph, rule syntax)
# only happens server-side, so glab needs an authenticated GitLab API.
# Only the operator's workstation has that: this check is the pre-push
# gate. CI runners carry no GitLab token, and don't need the check --
# there a broken pipeline file already fails loudly at pipeline creation.
if ! glab auth status >/dev/null 2>&1; then
  echo "lint:gitlab-ci: skipped, no authenticated glab (GitLab itself validates at pipeline creation)"
  exit 0
fi

glab ci lint .gitlab-ci.yml

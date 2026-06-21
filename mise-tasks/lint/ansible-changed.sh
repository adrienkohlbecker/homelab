#!/usr/bin/env bash
#MISE description="Run ansible-lint on YAML files changed vs origin/master (override via LINT_BASE)"
# Inner-loop variant of `lint:ansible`. Full lint loads + schema-validates every
# playbook in the repo and dominates `mise run lint` wall-clock; this scopes
# ansible-lint to just the YAML files touched on the current branch (committed
# diff vs origin/master + uncommitted + untracked). Falls back to uncommitted
# only when the base ref is missing (e.g. fresh clone with no remote fetched).
set -euo pipefail

# Silence ansible-core's own to_bytes/to_native deprecation spam (collection-internal
# imports, not our code) during lint. Scoped here, not in ansible.cfg, so real
# playbook runs still surface deprecations. Mirrors the lint:ansible task in mise.toml.
export ANSIBLE_DEPRECATION_WARNINGS=False

base="${LINT_BASE:-origin/master}"

files=$(
  {
    if git rev-parse --verify --quiet "$base" >/dev/null; then
      git diff --name-only --diff-filter=ACMR "$(git merge-base "$base" HEAD)"...HEAD -- '*.yml' '*.yaml'
    else
      echo "lint:ansible-changed: '$base' not found; scoping to uncommitted changes only" >&2
    fi
    git diff --name-only --diff-filter=ACMR HEAD -- '*.yml' '*.yaml'
    git ls-files --others --exclude-standard -- '*.yml' '*.yaml'
  } | sort -u | sed '/^$/d' |
    while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done
)

if [ -z "$files" ]; then
  echo "lint:ansible-changed: no changed YAML files."
  exit 0
fi

count=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
echo "lint:ansible-changed: linting $count file(s) vs $base"
printf '%s\n' "$files" | xargs ansible-lint

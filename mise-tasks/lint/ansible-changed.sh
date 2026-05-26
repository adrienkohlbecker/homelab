#!/usr/bin/env bash
#MISE description="Run ansible-lint on YAML files changed vs origin/master (override via LINT_BASE)"
# Inner-loop variant of `lint:ansible`. Full lint loads + schema-validates every
# playbook in the repo and dominates `mise run lint` wall-clock; this scopes
# ansible-lint to just the YAML files touched on the current branch (committed
# diff vs origin/master + uncommitted + untracked). Falls back to uncommitted
# only when the base ref is missing (e.g. fresh clone with no remote fetched).
set -euo pipefail

base="${LINT_BASE:-origin/master}"

if git rev-parse --verify --quiet "$base" >/dev/null; then
  merge_base=$(git merge-base "$base" HEAD)
  committed=$(git diff --name-only --diff-filter=ACMR "$merge_base"...HEAD -- '*.yml' '*.yaml')
else
  echo "lint:ansible-changed: '$base' not found; scoping to uncommitted changes only" >&2
  committed=""
fi
uncommitted=$(git diff --name-only --diff-filter=ACMR HEAD -- '*.yml' '*.yaml')
untracked=$(git ls-files --others --exclude-standard -- '*.yml' '*.yaml')

files=$(printf '%s\n%s\n%s\n' "$committed" "$uncommitted" "$untracked" | sort -u | sed '/^$/d')

if [ -z "$files" ]; then
  echo "lint:ansible-changed: no changed YAML files."
  exit 0
fi

count=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
echo "lint:ansible-changed: linting $count file(s) vs $base"
printf '%s\n' "$files" | xargs ansible-lint

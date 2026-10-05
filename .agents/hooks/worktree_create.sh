#!/usr/bin/env bash
# Agent WorktreeCreate hook: create a repo worktree and print only its path.
# Diagnostics must stay on stderr because stdout is the hook return value.
set -euo pipefail

# shellcheck source=mise-tasks/worktree/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../../mise-tasks/worktree/lib.sh"

input=$(cat)
cwd=$(jq -r '.cwd // empty' <<<"$input")
name=$(jq -r '.name // empty' <<<"$input")

[ -n "$cwd" ] || {
  echo "WorktreeCreate hook: missing cwd" >&2
  exit 1
}
[ -n "$name" ] || {
  echo "WorktreeCreate hook: missing name" >&2
  exit 1
}

repo=$(main_worktree "$cwd")
[ -n "$repo" ] || {
  echo "WorktreeCreate hook: cwd $cwd not in a git repo" >&2
  exit 1
}

# Base off the local default branch (whatever the main worktree has checked
# out), not origin - the operator often has unpushed local commits that the
# new worktree should include.
default_branch=$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null) || true
[ -n "$default_branch" ] || {
  echo "WorktreeCreate hook: cannot determine default branch (detached HEAD?)" >&2
  exit 1
}

wt="$repo/.worktrees/$name"
git -C "$repo" worktree add -b "$name" "$wt" "$default_branch" >&2

"$repo/mise-tasks/worktree/populate.sh" "$wt" >&2 ||
  echo "WorktreeCreate hook: populate failed (non-fatal)" >&2

echo "$wt"

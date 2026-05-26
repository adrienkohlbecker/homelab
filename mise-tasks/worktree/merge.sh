#!/usr/bin/env bash
#MISE description="Rebase <name> onto master, fast-forward master, then worktree:rm <name>"
#MISE alias="wt:merge"
#USAGE arg "<name>" help="Branch and worktree name"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Rebases <name> onto master first so the FF-only merge succeeds even if
# master has moved since the worktree was branched. Conflicts halt the
# script with set -e — the worktree is left in mid-rebase for resolution,
# rm doesn't run. Operates from the main worktree so that worktree:rm
# removing the caller's cwd (if invoked from inside the worktree being
# merged) doesn't strand the rest of the script.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
cd "$repo"
wt=".worktrees/$usage_name"
[ -d "$wt" ] || {
  echo "worktree:merge: $wt not found" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:merge: main worktree on '$main_branch', expected master" >&2
  exit 1
}
git -C "$wt" rebase master
git merge --ff-only "$usage_name"
mise run worktree:rm "$usage_name"

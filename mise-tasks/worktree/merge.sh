#!/usr/bin/env bash
#MISE description="Merge <name> into master: rebase it current, fast-forward master onto it, then worktree:rm <name>"
#MISE alias="wt:merge"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE complete "name" run="git worktree list --porcelain | awk '/^worktree .*\\/.worktrees\\//{n=split($2,a,\"/\"); print a[n]}'"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Integrates worktree branch <name> into master, keeping history strictly linear.
# notes/ is a shared gitignored clone (not tracked), so there is no submodule
# history to reconcile -- worktree:update rebases the code branch onto master, and
# master then fast-forwards onto it. The --ff-only guarantees no merge commit: if
# the rebase didn't make <name> a descendant of master (e.g. master moved during a
# manual conflict resolution), it halts rather than recording a merge.
#
# A conflict during the rebase halts (set -e) in worktree:update; resolve it there
# (git add, git rebase --continue) and re-run this task. Operates from the main
# worktree so worktree:rm removing the caller's cwd can't strand the script.
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

mise run worktree:update "$usage_name"
git merge --ff-only "$usage_name"
mise run worktree:rm "$usage_name"

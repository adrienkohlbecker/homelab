#!/usr/bin/env bash
#MISE description="Rebase worktree <name> onto master, keeping its history linear"
#MISE alias="wt:update"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE complete "name" run="git worktree list --porcelain | awk '/^worktree .*\\/.worktrees\\//{n=split($2,a,\"/\"); print a[n]}'"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Brings worktree branch <name> current with master -- the symmetric inverse of
# worktree:merge. notes/ is a shared gitignored clone (symlinked per worktree),
# not a tracked submodule, so the rebase has no submodule gitlink to reconcile:
# this is a plain `git rebase master`.
#
# A conflict halts (set -e) for manual resolution: fix it in the worktree
# (git add the files, git rebase --continue), then re-run worktree:merge.
# Rebases onto the local master ref, not origin/master -- refresh master in the
# main checkout first if you need it.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
cd "$repo"
wt=".worktrees/$usage_name"
[ -d "$wt" ] || {
  echo "worktree:update: $wt not found" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:update: main worktree on '$main_branch', expected master" >&2
  exit 1
}

git -C "$wt" rebase master

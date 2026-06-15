#!/usr/bin/env bash
#MISE description="Rebase a worktree onto master, keeping its history linear"
#MISE alias="wt:update"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec

# Brings a worktree branch current with master -- the symmetric inverse of
# worktree:merge. notes/ is a shared gitignored clone (symlinked per worktree),
# not a tracked submodule, so the rebase has no submodule gitlink to reconcile:
# this is a plain `git rebase master`.
#
# A conflict halts (set -e) for manual resolution: fix it in the worktree
# (git add the files, git rebase --continue), then re-run worktree:merge.
# Rebases onto the local master ref, not origin/master -- refresh master in the
# main checkout first if you need it.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')
cd "$repo"

# Resolve <worktree> -- a path (as tab-completed) or a branch name -- to the
# worktree's location, so it works wherever the worktree lives, not only under
# .worktrees/. A path is canonicalized via rev-parse --show-toplevel (matches
# git's stored form exactly, resolving symlinks and any subdir-of-worktree);
# path match wins (it also covers detached worktrees), branch is the fallback.
# substr keeps paths with spaces intact.
abs=""
if [ -d "$usage_worktree" ]; then
  abs=$(git -C "$usage_worktree" rev-parse --show-toplevel 2>/dev/null || true)
fi
wt=$(git -C "$repo" worktree list --porcelain | awk -v abs="$abs" -v b="refs/heads/$usage_worktree" '
  /^worktree / {p = substr($0, 10)}
  abs != "" && p == abs {print p; exit}
  $0 == "branch " b {print p; exit}')
[ -n "$wt" ] || {
  echo "worktree:update: no worktree found for '$usage_worktree'" >&2
  exit 1
}
[ "$wt" != "$repo" ] || {
  echo "worktree:update: refusing to operate on the main worktree" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:update: main worktree on '$main_branch', expected master" >&2
  exit 1
}

git -C "$wt" rebase master

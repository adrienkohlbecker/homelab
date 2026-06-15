#!/usr/bin/env bash
#MISE description="Remove a worktree and delete its branch"
#MISE alias="wt:rm"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')

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
  echo "worktree:rm: no worktree found for '$usage_worktree'" >&2
  exit 1
}
[ "$wt" != "$repo" ] || {
  echo "worktree:rm: refusing to operate on the main worktree" >&2
  exit 1
}
# Branch to delete after removal (empty for a detached worktree -- nothing to delete).
branch=$(git -C "$wt" symbolic-ref --quiet --short HEAD || true)

# Worktrees carry gitignored generated state -- the notes/, packer/artifacts,
# terraform/.terraform and .remember symlinks populate.sh creates -- which
# `git worktree remove` treats as blocking content, so --force is required to
# clear it. Guard with an explicit dirty-check first (status --porcelain ignores
# gitignored paths) so --force never silently discards uncommitted tracked work.
if [ -n "$(git -C "$wt" status --porcelain)" ]; then
  echo "worktree:rm: $wt has uncommitted changes; commit/stash or remove it manually" >&2
  exit 1
fi
git -C "$repo" worktree remove --force "$wt"
if [ -n "$branch" ]; then
  git -C "$repo" branch -D "$branch" 2>/dev/null || true
fi

#!/usr/bin/env bash
#MISE description="Merge a worktree into master: rebase it current, fast-forward master onto it, then worktree:rm it"
#MISE alias="wt:merge"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec

# Integrates a worktree into master, keeping history strictly linear.
# notes/ is a shared gitignored clone (not tracked), so there is no submodule
# history to reconcile -- worktree:update rebases the worktree onto master, and
# master then fast-forwards onto its post-rebase HEAD commit. Targeting the
# commit (not a branch ref) lets detached worktrees merge too. The --ff-only
# guarantees no merge commit: if the rebase didn't make HEAD a descendant of
# master (e.g. master moved during a manual conflict resolution), it halts
# rather than recording a merge.
#
# A conflict during the rebase halts (set -e) in worktree:update; resolve it there
# (git add, git rebase --continue) and re-run this task. Operates from the main
# worktree so worktree:rm removing the caller's cwd can't strand the script.
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
  echo "worktree:merge: no worktree found for '$usage_worktree'" >&2
  exit 1
}
[ "$wt" != "$repo" ] || {
  echo "worktree:merge: refusing to operate on the main worktree" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:merge: main worktree on '$main_branch', expected master" >&2
  exit 1
}

mise run worktree:update "$usage_worktree"
# Fast-forward master onto the worktree's post-rebase HEAD commit. Using the
# commit rather than a branch ref works for detached worktrees too; for a
# branched worktree it is the branch tip all the same.
git merge --ff-only "$(git -C "$wt" rev-parse HEAD)"
mise run worktree:rm "$usage_worktree"

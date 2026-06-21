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

# shellcheck source=mise-tasks/worktree/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

repo=$(main_worktree)
wt=$(resolve_side_worktree "$repo" "$usage_worktree")
require_main_master "$repo"

git -C "$wt" rebase master
# Fast-forward master onto the worktree's post-rebase HEAD commit. Using the
# commit rather than a branch ref works for detached worktrees too; for a
# branched worktree it is the branch tip all the same.
git -C "$repo" merge --ff-only "$(git -C "$wt" rev-parse HEAD)"
remove_clean_worktree "$repo" "$wt"

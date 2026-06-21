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

# shellcheck source=mise-tasks/worktree/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

repo=$(main_worktree)
wt=$(resolve_side_worktree "$repo" "$usage_worktree")
require_main_master "$repo"

git -C "$wt" rebase master

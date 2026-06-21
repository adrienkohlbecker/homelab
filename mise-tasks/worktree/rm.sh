#!/usr/bin/env bash
#MISE description="Remove a worktree and delete its branch"
#MISE alias="wt:rm"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=mise-tasks/worktree/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

repo=$(main_worktree)
wt=$(resolve_side_worktree "$repo" "$usage_worktree")
remove_clean_worktree "$repo" "$wt"

#!/usr/bin/env bash
#MISE description="Merge a worktree into master, then remove it and its branch"
#MISE alias="wt:finish"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"

# worktree:merge followed by worktree:rm. Both read the same exported
# usage_worktree, and set -e stops before the removal if the merge halts
# (rebase conflict, main worktree off master), so an unmerged worktree is
# never removed.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"$here/merge.sh"
"$here/rm.sh"

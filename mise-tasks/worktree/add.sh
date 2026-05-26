#!/usr/bin/env bash
#MISE description="Create .worktrees/<name>, populate shared state, mise trust, open VS Code"
#MISE alias="wt:add"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE arg "[base]" default="HEAD" help="Base commit-ish"
# shellcheck disable=SC2154  # usage_name / usage_base injected by mise from the #USAGE spec

# The populate step (packer/artifacts symlink + .worktreeinclude copies +
# mise trust) is factored into mise-tasks/worktree/populate so the same
# logic runs from Claude Code's WorktreeCreate hook (.claude/settings.json).
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
wt="$repo/.worktrees/$usage_name"
git -C "$repo" worktree add -b "$usage_name" "$wt" "$usage_base"
"$repo/mise-tasks/worktree/populate.sh" "$wt"
code --new-window "$wt"

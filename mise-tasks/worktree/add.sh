#!/usr/bin/env bash
#MISE description="Create a worktree (default .worktrees/<name>), populate shared state, mise trust, open VS Code"
#MISE alias="wt:add"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE arg "[base]" default="HEAD" help="Base commit-ish"
#USAGE complete "base" run="echo HEAD; git branch --format='%(refname:short)'"
#USAGE flag "--path <path>" help="Worktree location (default: <repo>/.worktrees/<name>)"
# shellcheck disable=SC2154  # usage_name / usage_base / usage_path injected by mise from the #USAGE spec

# The populate step (packer/artifacts symlink + .worktreeinclude copies +
# mise trust) is factored into mise-tasks/worktree/populate so the same
# logic runs from Claude Code's WorktreeCreate hook (.claude/settings.json).
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')

# Worktree location: --path wins (relative paths resolve against the caller's
# cwd, absolute taken verbatim); otherwise the conventional .worktrees/<name>.
case "${usage_path:-}" in
'') wt="$repo/.worktrees/$usage_name" ;;
/*) wt="$usage_path" ;;
*) wt="$PWD/$usage_path" ;;
esac

git -C "$repo" worktree add -b "$usage_name" "$wt" "$usage_base"
wt=$(cd "$wt" && pwd)
"$repo/mise-tasks/worktree/populate.sh" "$wt"
code --new-window "$wt"

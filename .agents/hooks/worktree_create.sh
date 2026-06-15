#!/usr/bin/env bash
# Claude Code WorktreeCreate hook: create the worktree under <repo>/.worktrees/
# and populate it (notes/packer/artifacts symlinks etc. via mise-tasks/worktree/populate.sh).
# The new worktree path is the hook's return value, so it is the ONLY thing on
# stdout -- every diagnostic goes to stderr.
#
# EnterWorktree sends: { cwd, name, session_id, transcript_path, hook_event_name }
# The hook derives the base from the local default branch (HEAD of the main
# worktree) and creates a new branch named after the worktree.
set -euo pipefail

input=$(cat)
cwd=$(jq -r '.cwd // empty' <<<"$input")
name=$(jq -r '.name // empty' <<<"$input")

for v in cwd name; do
  [ -n "${!v}" ] || {
    echo "WorktreeCreate hook: missing $v" >&2
    exit 1
  }
done

repo=$(git -C "$cwd" worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')
[ -n "$repo" ] || {
  echo "WorktreeCreate hook: cwd $cwd not in a git repo" >&2
  exit 1
}

# Base off the local default branch (whatever the main worktree has checked
# out), not origin — the operator often has unpushed local commits that the
# new worktree should include.
default_branch=$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null) || true
[ -n "$default_branch" ] || {
  echo "WorktreeCreate hook: cannot determine default branch (detached HEAD?)" >&2
  exit 1
}

base="$default_branch"
new="$name"

wt="$repo/.worktrees/$name"
git -C "$repo" worktree add -b "$new" "$wt" "$base" >&2

if [ -x "$repo/mise-tasks/worktree/populate.sh" ]; then
  "$repo/mise-tasks/worktree/populate.sh" "$wt" >&2 ||
    echo "WorktreeCreate hook: populate failed (non-fatal)" >&2
fi

echo "$wt"

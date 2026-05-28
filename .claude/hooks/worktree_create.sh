#!/usr/bin/env bash
# Claude Code WorktreeCreate hook: create the worktree under <repo>/.worktrees/
# and populate it (notes submodule init etc. via mise-tasks/worktree/populate.sh).
# The new worktree path is the hook's return value, so it is the ONLY thing on
# stdout -- every diagnostic goes to stderr.
set -euo pipefail

input=$(cat)
cwd=$(jq -r '.cwd // empty' <<<"$input")
name=$(jq -r '.worktree_name // empty' <<<"$input")
base=$(jq -r '.base_branch // empty' <<<"$input")
new=$(jq -r '.new_branch // empty' <<<"$input")

for v in cwd name base new; do
  [ -n "${!v}" ] || {
    echo "WorktreeCreate hook: missing $v" >&2
    exit 1
  }
done

repo=$(git -C "$cwd" worktree list --porcelain | awk '/^worktree / {print $2; exit}')
[ -n "$repo" ] || {
  echo "WorktreeCreate hook: cwd $cwd not in a git repo" >&2
  exit 1
}

wt="$repo/.worktrees/$name"
# new==base means a detached worktree (no parent branch); otherwise branch off base.
if [ "$new" = "$base" ]; then
  git -C "$repo" worktree add --detach "$wt" "$base" >&2
else
  git -C "$repo" worktree add -b "$new" "$wt" "$base" >&2
fi

if [ -x "$repo/mise-tasks/worktree/populate.sh" ]; then
  "$repo/mise-tasks/worktree/populate.sh" "$wt" >&2 ||
    echo "WorktreeCreate hook: populate failed (non-fatal)" >&2
fi

echo "$wt"

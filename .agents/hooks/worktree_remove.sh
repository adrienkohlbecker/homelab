#!/usr/bin/env bash
# Claude Code WorktreeRemove hook: tear down a worktree created by
# worktree_create.sh and delete its branch.
#
# Before any destructive step, validate that worktree_path is a registered
# worktree physically under <repo>/.worktrees/ -- a crafted worktree_path two
# levels below any directory containing a .git would otherwise let the
# --force remove / branch -D below discard an unrelated checkout or branch.
# --force is retained deliberately: by the time this fires the worktree tooling
# (worktree:merge) has already reconciled the notes/ submodule, and this is
# isolation-cleanup of a known-ours worktree, so refusing on a dirty tree would
# only strand it.
set -euo pipefail

input=$(cat)
new=$(jq -r '.new_branch // empty' <<<"$input")
base=$(jq -r '.base_branch // empty' <<<"$input")
wt=$(jq -r '.worktree_path // empty' <<<"$input")

[ -n "$wt" ] || exit 0
main=$(dirname "$(dirname "$wt")")
[ -d "$main/.git" ] || exit 0

case "$wt" in
"$main/.worktrees/"*) ;;
*)
  echo "WorktreeRemove hook: $wt not under $main/.worktrees/ -- skipping" >&2
  exit 0
  ;;
esac

if ! git -C "$main" worktree list --porcelain | grep -qxF "worktree $wt"; then
  echo "WorktreeRemove hook: $wt is not a registered worktree -- skipping" >&2
  exit 0
fi

git -C "$main" worktree remove --force "$wt" || true
git -C "$main" worktree prune || true
if [ -n "$new" ] && [ "$new" != "$base" ]; then
  git -C "$main" branch -D "$new" || true
fi

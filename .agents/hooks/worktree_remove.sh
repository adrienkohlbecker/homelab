#!/usr/bin/env bash
# Claude Code WorktreeRemove hook: tear down a worktree created by
# worktree_create.sh and delete its branch. Works wherever the worktree lives,
# not only under <repo>/.worktrees/.
#
# Before any destructive step, confirm git itself reports worktree_path as a
# registered worktree of this repo (and not the main worktree) -- that check is
# the real guard against a crafted worktree_path letting the --force remove /
# branch -D below discard an unrelated checkout or branch. --force is retained
# deliberately: this is isolation-cleanup of a known-ours worktree, so refusing
# on a dirty tree (gitignored populate symlinks count as content) would only
# strand it.
set -euo pipefail

input=$(cat)
new=$(jq -r '.new_branch // empty' <<<"$input")
base=$(jq -r '.base_branch // empty' <<<"$input")
wt=$(jq -r '.worktree_path // empty' <<<"$input")

[ -n "$wt" ] || exit 0

# Canonicalise to git's stored worktree path (resolves symlinks / any subdir),
# so the registered-worktree check below matches exactly. Bails if the path is
# already gone or not a git worktree.
wt=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Resolve the main worktree from the worktree itself (first `worktree list`
# entry is always the real repo), so an arbitrary worktree location is fine.
main=$(git -C "$wt" worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')
[ -n "$main" ] || exit 0
[ "$wt" != "$main" ] || {
  echo "WorktreeRemove hook: $wt is the main worktree -- skipping" >&2
  exit 0
}

if ! git -C "$main" worktree list --porcelain | grep -qxF "worktree $wt"; then
  echo "WorktreeRemove hook: $wt is not a registered worktree -- skipping" >&2
  exit 0
fi

git -C "$main" worktree remove --force "$wt" || true
git -C "$main" worktree prune || true
if [ -n "$new" ] && [ "$new" != "$base" ]; then
  git -C "$main" branch -D "$new" || true
fi

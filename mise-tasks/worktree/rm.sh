#!/usr/bin/env bash
#MISE description="Remove a worktree and delete its branch"
#MISE alias="wt:rm"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE complete "name" run="git worktree list --porcelain | awk '/^worktree .*\\/.worktrees\\//{n=split($2,a,\"/\"); print a[n]}'"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Before deleting the worktree, migrate Claude Code session logs from
# ~/.claude/projects/<encoded-worktree-path>/ to the main repo's project
# dir and rewrite the embedded worktree path inside each JSONL so the
# transcripts remain navigable after the worktree is gone. Claude Code
# encodes the absolute project path by replacing `/` with `-`.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
wt="$repo/.worktrees/$usage_name"
enc_main="${repo//\//-}"
enc_wt="${wt//\//-}"
src_proj="$HOME/.claude/projects/$enc_wt"
dst_proj="$HOME/.claude/projects/$enc_main"
if [ -d "$src_proj" ]; then
  n=$(find "$src_proj" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then
    mkdir -p "$dst_proj"
    find "$src_proj" -maxdepth 1 -name '*.jsonl' \
      -exec perl -i -pe "s|\Q$wt\E|$repo|g" {} +
    find "$src_proj" -maxdepth 1 -name '*.jsonl' \
      -exec mv {} "$dst_proj/" +
    echo "Migrated $n Claude session(s); rewrote $wt -> $repo"
  fi
  rmdir "$src_proj" 2>/dev/null || true
fi
# git refuses to remove a worktree containing a submodule (notes) even when
# clean, so --force is required to clear that structural guard. Guard with an
# explicit dirty-check first so --force only bypasses the submodule guard and
# never silently discards uncommitted work (code or notes).
if [ -n "$(git -C "$wt" status --porcelain)" ]; then
  echo "worktree:rm: $wt has uncommitted changes; commit/stash or remove it manually" >&2
  exit 1
fi
# If notes was registered as a worktree of the main notes checkout, remove it
# first so the notes gitdir stays consistent.  The subsequent --force on the
# parent removal then only needs to override the submodule-path guard for any
# old-style submodule clone that wasn't migrated.
if git -C "$repo/notes" worktree list --porcelain 2>/dev/null | grep -qxF "worktree $wt/notes"; then
  git -C "$repo/notes" worktree remove "$wt/notes"
fi
git -C "$repo" worktree remove --force "$wt"
git -C "$repo" branch -D "$usage_name" 2>/dev/null || true

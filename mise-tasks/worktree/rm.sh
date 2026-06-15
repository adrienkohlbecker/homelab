#!/usr/bin/env bash
#MISE description="Remove a worktree and delete its branch"
#MISE alias="wt:rm"
#USAGE arg "<worktree>" help="Worktree path or branch name"
#USAGE complete "worktree" run="git worktree list --porcelain | awk '/^worktree /{n++; if(n>1) print substr($0,10)}'"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec

# Before deleting the worktree, migrate Claude Code session logs from
# ~/.claude/projects/<encoded-worktree-path>/ to the main repo's project
# dir and rewrite the embedded worktree path inside each JSONL so the
# transcripts remain navigable after the worktree is gone. Claude Code
# encodes the absolute project path by replacing both `/` and `.` with `-`
# (so .worktrees/x -> --worktrees-x); the encoding must match exactly or the
# source dir is never found and the migration silently no-ops.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}')

# Resolve <worktree> -- a path (as tab-completed) or a branch name -- to the
# worktree's location, so it works wherever the worktree lives, not only under
# .worktrees/. A path is canonicalized via rev-parse --show-toplevel (matches
# git's stored form exactly, resolving symlinks and any subdir-of-worktree);
# path match wins (it also covers detached worktrees), branch is the fallback.
# substr keeps paths with spaces intact.
abs=""
if [ -d "$usage_worktree" ]; then
  abs=$(git -C "$usage_worktree" rev-parse --show-toplevel 2>/dev/null || true)
fi
wt=$(git -C "$repo" worktree list --porcelain | awk -v abs="$abs" -v b="refs/heads/$usage_worktree" '
  /^worktree / {p = substr($0, 10)}
  abs != "" && p == abs {print p; exit}
  $0 == "branch " b {print p; exit}')
[ -n "$wt" ] || {
  echo "worktree:rm: no worktree found for '$usage_worktree'" >&2
  exit 1
}
[ "$wt" != "$repo" ] || {
  echo "worktree:rm: refusing to operate on the main worktree" >&2
  exit 1
}
# Branch to delete after removal (empty for a detached worktree -- nothing to delete).
branch=$(git -C "$wt" symbolic-ref --quiet --short HEAD || true)

enc_main="${repo//[\/.]/-}"
enc_wt="${wt//[\/.]/-}"
src_proj="$HOME/.claude/projects/$enc_wt"
dst_proj="$HOME/.claude/projects/$enc_main"
if [ -d "$src_proj" ]; then
  n=$(find "$src_proj" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')
  mkdir -p "$dst_proj"
  # Rewrite the worktree path to the main repo path across every file -- sessions,
  # nested subagent transcripts, and tool-result captures all embed it. `{}` must
  # be the last token before `+`, so the rewrite and the move are separate finds.
  find "$src_proj" -type f -exec perl -i -pe "s|\Q$wt\E|$repo|g" {} +
  # Move each top-level entry (UUID .jsonl files and UUID/ session dirs) into the
  # main project dir. Names are session-UUID unique, so nothing clobbers. The
  # \; form is required here because an argument follows {}.
  find "$src_proj" -mindepth 1 -maxdepth 1 -exec mv -f {} "$dst_proj/" \;
  rmdir "$src_proj" 2>/dev/null || true
  if [ "$n" -gt 0 ]; then
    echo "Migrated $n Claude session(s); rewrote $wt -> $repo"
  fi
fi
# Worktrees carry gitignored generated state -- the notes/, packer/artifacts,
# terraform/.terraform and .remember symlinks populate.sh creates -- which
# `git worktree remove` treats as blocking content, so --force is required to
# clear it. Guard with an explicit dirty-check first (status --porcelain ignores
# gitignored paths) so --force never silently discards uncommitted tracked work.
if [ -n "$(git -C "$wt" status --porcelain)" ]; then
  echo "worktree:rm: $wt has uncommitted changes; commit/stash or remove it manually" >&2
  exit 1
fi
git -C "$repo" worktree remove --force "$wt"
if [ -n "$branch" ]; then
  git -C "$repo" branch -D "$branch" 2>/dev/null || true
fi

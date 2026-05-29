#!/usr/bin/env bash
#MISE description="Rebase worktree <name> onto master: rebase its notes branch onto master:notes first, then rebase the code branch, remapping notes-gitlink conflicts to the rebased commits so code and notes history stay consistent"
#MISE alias="wt:update"
#USAGE arg "<name>" help="Branch and worktree name"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Brings worktree branch <name> current with master (the symmetric inverse of
# worktree:merge). Keeping the code history and the notes-submodule history
# consistent across the rebase is the whole job:
#
#   master may have advanced BOTH its code and the commit it records for the
#   notes submodule (master:notes). The worktree, meanwhile, may carry its own
#   notes commits plus code commits that bump the notes gitlink. Those two notes
#   lines are then DIVERGENT -- siblings off a common base, neither an ancestor
#   of the other.
#
# Plain `git rebase master` cannot merge a divergent submodule gitlink: it halts
# with "Failed to merge submodule" at every gitlink-bump commit. But git DOES
# auto-resolve a gitlink when one side fast-forwards the other, so the fix is:
#
#   1. Rebase the worktree's notes branch onto master:notes first. Its own notes
#      commits replay on top, gaining new SHAs that ARE descendants of
#      master:notes. Record an old->new SHA map.
#   2. Rebase the code branch. At each notes-gitlink conflict the conflicting
#      "theirs" (index stage 3) is the pre-rebase, divergent notes SHA; repoint
#      it to the mapped new SHA (a descendant of master:notes) and continue.
#      Every code commit then pins a notes commit that exists on the rebased
#      notes branch, and the worktree ends with a clean `git status`.
#
# A conflict on anything other than the notes gitlink halts for manual handling.
# Rebases onto the local master ref, not origin/master -- refresh master in the
# main checkout first if you need it.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
cd "$repo"
wt=".worktrees/$usage_name"
[ -d "$wt" ] || {
  echo "worktree:update: $wt not found" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:update: main worktree on '$main_branch', expected master" >&2
  exit 1
}

# The commit master records for the notes submodule. Empty when the repo has no
# notes submodule -> skip the notes step; the code rebase still runs.
master_notes=$(git rev-parse -q --verify master:notes 2>/dev/null || true)

# Step 1: rebase the worktree's notes branch onto master:notes, recording each
# rewritten own-commit's old SHA -> new SHA as a file under $mapdir. Skipped for
# detached/agent worktrees (new==base), which have no <name> notes branch.
wt_notes="$repo/$wt/notes"
mapdir=""
if [ -n "$master_notes" ] && [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  git -C "$wt_notes" fetch -q origin
  # master:notes must be an object this clone has in order to rebase onto it --
  # clones exchange via origin or a direct fetch from the main checkout's notes.
  git -C "$wt_notes" fetch -q "$repo/notes" "$master_notes" 2>/dev/null || true
  base=$(git -C "$wt_notes" merge-base "$usage_name" "$master_notes")
  old_own=$(git -C "$wt_notes" rev-list --reverse "$base".."$usage_name")
  git -C "$wt_notes" rebase -q "$master_notes" "$usage_name"
  new_own=$(git -C "$wt_notes" rev-list --reverse "$master_notes".."$usage_name")
  if [ "$(printf '%s\n' "$old_own" | grep -c .)" != "$(printf '%s\n' "$new_own" | grep -c .)" ]; then
    echo "worktree:update: notes rebase changed the own-commit count (an own commit went empty?); resolve notes manually" >&2
    exit 1
  fi
  mapdir=$(mktemp -d)
  # old<space>new per line; identity rows (master:notes hadn't moved) and the
  # empty-input row (no own commits) are skipped. The body must stay an `if`, not
  # an `&&`-chain: a trailing failed test would become the pipeline's exit status
  # under pipefail and trip `set -e`.
  paste -d' ' <(printf '%s\n' "$old_own") <(printf '%s\n' "$new_own") | while read -r o n; do
    if [ -n "$o" ] && [ -n "$n" ] && [ "$o" != "$n" ]; then
      printf '%s' "$n" >"$mapdir/$o"
    fi
  done
fi

# Step 2: rebase the code branch, auto-resolving notes-gitlink conflicts to the
# mapped (descendant-of-master:notes) SHAs.
export GIT_EDITOR=true # rebase --continue must not block on an editor
gd=$(git -C "$wt" rev-parse --absolute-git-dir)
git -C "$wt" -c advice.submoduleMergeConflict=false -c advice.mergeConflict=false rebase master || true
guard=0
while [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; do
  guard=$((guard + 1))
  if [ "$guard" -gt 1000 ]; then
    echo "worktree:update: rebase auto-resolve is not progressing; aborting (resolve manually in $wt)" >&2
    exit 1
  fi
  conflicts=$(git -C "$wt" diff --name-only --diff-filter=U)
  if [ "$conflicts" = "notes" ] && [ -n "$mapdir" ]; then
    # stage 3 (theirs) is the commit being replayed -> its pre-rebase notes SHA.
    theirs=$(git -C "$wt" rev-parse ":3:notes")
    new=$(cat "$mapdir/$theirs" 2>/dev/null || true)
    if [ -z "$new" ]; then
      echo "worktree:update: notes-gitlink conflict on an unmapped commit ($theirs); resolve manually (map in $mapdir)" >&2
      exit 1
    fi
    git -C "$wt" update-index --cacheinfo "160000,$new,notes"
    git -C "$wt" -c advice.submoduleMergeConflict=false -c advice.mergeConflict=false rebase --continue || true
  else
    echo "worktree:update: rebase halted on a conflict beyond the notes gitlink:" >&2
    git -C "$wt" status --short >&2
    [ -n "$mapdir" ] && echo "  (remaining notes-gitlink remaps, if any, are under $mapdir)" >&2
    echo "  Resolve in $wt, then 'git -C $wt rebase --continue'." >&2
    exit 1
  fi
done

if [ -n "$mapdir" ]; then rm -rf "$mapdir"; fi

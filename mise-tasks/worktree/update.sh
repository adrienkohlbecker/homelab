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
#      master:notes. Index them by patch-id -> new SHA.
#   2. Rebase the code branch. At each notes-gitlink conflict the conflicting
#      "theirs" (index stage 3) is the pre-rebase notes SHA a code commit pins;
#      look up its patch-id to find the rebased twin (same patch-id, since a
#      plain rebase preserves diffs) and repoint the gitlink to it, then
#      continue. Every code commit then pins a notes commit that exists on the
#      rebased notes branch, and the worktree ends with a clean `git status`.
#
# Keying the map on patch-id rather than on step 1's old->new rewrite makes
# resolution independent of whether step 1 actually moved anything: if the notes
# branch is ALREADY rebased onto master:notes (e.g. a re-run after a halted code
# rebase left the notes branch rewritten but the code branch not), step 1 is a
# no-op, yet the code commits still pin the pre-rebase notes SHAs -- which share
# a patch-id with their twins on the branch, so they still resolve.
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

# Step 1: rebase the worktree's notes branch onto master:notes, then index each
# resulting own notes commit by patch-id -> its SHA under $mapdir. Skipped for
# detached/agent worktrees (new==base), which have no <name> notes branch.
wt_notes="$repo/$wt/notes"
mapdir=""
if [ -n "$master_notes" ] && [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  git -C "$wt_notes" fetch -q origin
  # master:notes must be an object this clone has in order to rebase onto it --
  # clones exchange via origin or a direct fetch from the main checkout's notes.
  git -C "$wt_notes" fetch -q "$repo/notes" "$master_notes" 2>/dev/null || true
  git -C "$wt_notes" rebase -q "$master_notes" "$usage_name"
  mapdir=$(mktemp -d)
  # Key by patch-id (stable across the rebase) so the map reflects the branch as
  # it stands now, not the rewrite step 1 happened to perform -- see the header.
  # An own commit with no patch-id (an empty commit) just isn't indexed; a code
  # commit that pins it then halts as "unmapped", which is the right call.
  for n in $(git -C "$wt_notes" rev-list "$master_notes".."$usage_name"); do
    pid=$(git -C "$wt_notes" show --no-color "$n" | git patch-id --stable | awk '{print $1}' || true)
    if [ -n "$pid" ]; then printf '%s' "$n" >"$mapdir/$pid"; fi
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
    # stage 3 (theirs) is the notes SHA the replayed code commit pins (pre-rebase);
    # find its rebased twin on the branch by patch-id. Fetch it into the notes
    # clone first if a prior gc/rebase left it absent there.
    theirs=$(git -C "$wt" rev-parse ":3:notes")
    git -C "$wt_notes" cat-file -e "$theirs" 2>/dev/null || git -C "$wt_notes" fetch -q "$repo/notes" "$theirs" 2>/dev/null || true
    pid=$(git -C "$wt_notes" show --no-color "$theirs" 2>/dev/null | git patch-id --stable | awk '{print $1}' || true)
    new=""
    if [ -n "$pid" ] && [ -f "$mapdir/$pid" ]; then new=$(cat "$mapdir/$pid"); fi
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

#!/usr/bin/env bash
#MISE description="Rebase worktree <name> onto master: rebase its notes branch onto master:notes first, then rebase the code branch, remapping notes-gitlink conflicts to the rebased commits so code and notes history stay consistent"
#MISE alias="wt:update"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE complete "name" run="git worktree list --porcelain | awk '/^worktree .*\\/.worktrees\\//{n=split($2,a,\"/\"); print a[n]}'"
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
#      master:notes. Capture the old->new rewrite as a persistent map.
#   2. Rebase the code branch. At each notes-gitlink conflict the conflicting
#      "theirs" (index stage 3) is the pre-rebase notes SHA a code commit pins;
#      look it up in the map to find its rebased twin and repoint the gitlink to
#      it, then continue. Every code commit then pins a notes commit that exists
#      on the rebased notes branch, and the worktree ends with a clean status.
#
# The map is keyed by the pre-rebase SHA, captured positionally (a plain rebase
# preserves commit order and count). That survives a rebase that CHANGES a
# commit's diff -- which happens whenever master:notes touched the same files,
# shifting the commit's patch-id. Two fallbacks back it up: a patch-id index for
# a count mismatch from a dropped empty commit (the twin's diff is unchanged, so
# its patch-id still matches), and a subject bridge for an orphaned pin -- a SHA
# absent from the map under both its SHA and its (shifted) patch-id, matched to
# its twin by unique subject across the notes branch's full reachable history
# (the twin may live inside master:notes itself, e.g. once the worktree's notes
# work has been merged straight into master:notes).
#
# The map persists under the worktree's git dir, not a mktemp, so a run halted on
# a genuine (non-gitlink) conflict can be resumed: the operator resolves the
# conflict and re-runs this task, which reuses the map rather than rebuilding it
# from a notes branch that step 1 has already rewritten (the originals are gone
# from the range by then). A fresh run rebuilds the map from scratch, so a stale
# leftover from an aborted run never carries over.
#
# A conflict on anything other than the notes gitlink halts for manual handling;
# resolve it in the worktree and re-run this task to finish.
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

# The worktree's git dir is the stable anchor for the persistent old->new map.
gd=$(git -C "$wt" rev-parse --absolute-git-dir)
mapdir="$gd/wt-update-notesmap"
wt_notes="$repo/$wt/notes"

# Are we resuming a rebase a prior invocation started (halted on a genuine
# conflict the operator then resolved)? If so, step 1 already rewrote the notes
# branch and built the map -- reuse it. Otherwise this is a fresh start, so
# (re)build the map from scratch, discarding any stale leftover from an aborted
# or interrupted run (which could be empty or built against a different master).
resuming=false
if [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; then resuming=true; fi

# Step 1: rebase the worktree's notes branch onto master:notes and record the
# old->new SHA map under $mapdir. Skipped for detached/agent worktrees (which
# have no <name> notes branch). Built fresh on a new run, reused on a resume.
if [ -n "$master_notes" ] && [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  git -C "$wt_notes" fetch -q origin
  # master:notes must be an object this clone has in order to rebase onto it --
  # clones exchange via origin or a direct fetch from the main checkout's notes.
  git -C "$wt_notes" fetch -q "$repo/notes" "$master_notes" 2>/dev/null || true
  if [ "$resuming" = false ]; then
    # Fresh start: discard any stale leftover map, then rebuild.
    rm -rf "$mapdir"
    # Capture the own notes commits BEFORE rewriting -- these pre-rebase SHAs are
    # exactly what a code commit's gitlink pins (stage 3 at the conflict).
    old=$(git -C "$wt_notes" rev-list --reverse "$master_notes".."$usage_name")
    git -C "$wt_notes" rebase -q "$master_notes" "$usage_name"
    new=$(git -C "$wt_notes" rev-list --reverse "$master_notes".."$usage_name")
    mkdir -p "$mapdir"
    # Positional old->new map: file named for the pre-rebase SHA holds its twin.
    if [ "$(printf '%s\n' "$old" | grep -c .)" = "$(printf '%s\n' "$new" | grep -c .)" ]; then
      paste <(printf '%s\n' "$old") <(printf '%s\n' "$new") | while IFS=$'\t' read -r o n; do
        if [ -n "$o" ] && [ -n "$n" ]; then printf '%s' "$n" >"$mapdir/$o"; fi
      done
    fi
    # Patch-id fallback (pid-<id> -> twin), for a count mismatch from a dropped
    # empty commit where the positional pairing would be off by one.
    for n in $(git -C "$wt_notes" rev-list "$master_notes".."$usage_name"); do
      pid=$(git -C "$wt_notes" show --no-color "$n" | git patch-id --stable | awk '{print $1}' || true)
      if [ -n "$pid" ]; then printf '%s' "$n" >"$mapdir/pid-$pid"; fi
    done
  else
    # Resume: the map persists from the first run and the notes branch is already
    # rewritten. Re-assert the rebase (idempotent no-op) without rebuilding the
    # map -- the original SHAs are no longer in the range.
    git -C "$wt_notes" rebase -q "$master_notes" "$usage_name"
  fi
fi

# Has the worktree's notes work fully landed in master:notes already (e.g. merged
# straight in), leaving nothing above it? Then the notes lines have converged and
# every gitlink resolves to master:notes -- which also leaves the submodule
# checkout (at the branch tip == master:notes) matching the pin, so a clean status.
notes_converged=false
if [ -n "$master_notes" ] && [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  if [ -z "$(git -C "$wt_notes" rev-list "$master_notes".."$usage_name")" ]; then notes_converged=true; fi
fi

# Step 2: rebase the code branch, auto-resolving notes-gitlink conflicts to the
# mapped (descendant-of-master:notes) SHAs.
export GIT_EDITOR=true # rebase --continue must not block on an editor
# Start the rebase only if one isn't already underway -- a second "rebase master"
# would fatal on the existing rebase-merge dir left by a halted prior run.
if [ ! -d "$gd/rebase-merge" ] && [ ! -d "$gd/rebase-apply" ]; then
  git -C "$wt" -c advice.submoduleMergeConflict=false -c advice.mergeConflict=false rebase master || true
fi
guard=0
while [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; do
  guard=$((guard + 1))
  if [ "$guard" -gt 1000 ]; then
    echo "worktree:update: rebase auto-resolve is not progressing; aborting (resolve manually in $wt)" >&2
    exit 1
  fi
  conflicts=$(git -C "$wt" diff --name-only --diff-filter=U)
  if [ -z "$conflicts" ]; then
    # No unmerged paths: a resolution (auto here, or manual before a resume) is
    # staged and the rebase just needs to advance.
    git -C "$wt" -c advice.submoduleMergeConflict=false -c advice.mergeConflict=false rebase --continue || true
  elif [ "$conflicts" = "notes" ] && [ -d "$mapdir" ]; then
    # stage 3 (theirs) is the pre-rebase notes SHA the replayed code commit pins;
    # look up its rebased twin in the map.
    theirs=$(git -C "$wt" rev-parse ":3:notes")
    new=""
    if [ "$notes_converged" = true ]; then
      # Notes converged into master:notes -- pin straight to its tip.
      new="$master_notes"
    elif [ -f "$mapdir/$theirs" ]; then
      new=$(cat "$mapdir/$theirs")
    else
      # Fallback by patch-id (twin's diff unchanged by the rebase). Fetch the SHA
      # into the notes clone first if a prior gc/rebase left it absent there.
      git -C "$wt_notes" cat-file -e "$theirs" 2>/dev/null || git -C "$wt_notes" fetch -q "$repo/notes" "$theirs" 2>/dev/null || true
      pid=$(git -C "$wt_notes" show --no-color "$theirs" 2>/dev/null | git patch-id --stable | awk '{print $1}' || true)
      if [ -n "$pid" ] && [ -f "$mapdir/pid-$pid" ]; then new=$(cat "$mapdir/pid-$pid"); fi
      if [ -z "$new" ]; then
        # Tier 3: an orphaned pin -- an EARLIER notes rebase (e.g. before an
        # aborted update), or the notes work having been merged straight into
        # master:notes, leaves it absent from the map by SHA with its patch-id
        # shifted. Match by subject against the notes branch's full reachable
        # history (the twin may live inside master:notes itself) -- only when the
        # subject is unique there, so a wrong remap can't slip through.
        subj=$(git -C "$wt_notes" show -s --format=%s "$theirs" 2>/dev/null || true)
        if [ -n "$subj" ]; then
          bridge=$(git -C "$wt_notes" log --no-color --format='%H%x09%s' "$usage_name" | awk -F'\t' -v s="$subj" '$2 == s {print $1}')
          if [ "$(printf '%s\n' "$bridge" | grep -c .)" = 1 ]; then new="$bridge"; fi
        fi
      fi
    fi
    if [ -z "$new" ]; then
      echo "worktree:update: notes-gitlink conflict on an unmapped commit ($theirs); resolve manually (map in $mapdir)" >&2
      exit 1
    fi
    git -C "$wt" update-index --cacheinfo "160000,$new,notes"
    git -C "$wt" -c advice.submoduleMergeConflict=false -c advice.mergeConflict=false rebase --continue || true
  else
    echo "worktree:update: rebase halted on a conflict beyond the notes gitlink:" >&2
    git -C "$wt" status --short >&2
    [ -d "$mapdir" ] && echo "  (notes-gitlink remaps are cached under $mapdir)" >&2
    echo "  Resolve in $wt (git add the files), then re-run 'mise run wt:update $usage_name' to finish." >&2
    exit 1
  fi
done

if [ -d "$mapdir" ]; then rm -rf "$mapdir"; fi

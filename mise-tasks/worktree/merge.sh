#!/usr/bin/env bash
#MISE description="Merge <name> into master: rebase its notes onto master's notes ref, FF when possible (--no-ff only if origin/main diverged); then worktree:rm <name>"
#MISE alias="wt:merge"
#USAGE arg "<name>" help="Branch and worktree name"
#USAGE complete "name" run="git worktree list --porcelain | awk '/^worktree .*\\/.worktrees\\//{n=split($2,a,\"/\"); print a[n]}'"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Integrates the worktree branch <name> into master, keeping history linear
# whenever it safely can. The worktree's notes branch is first rebased onto the
# commit master currently records for the notes submodule (master:notes): it was
# forked from whatever master pointed at when the worktree was created, which a
# concurrent merge or an operator bump may have since moved past. After that
# rebase:
#   - code-only worktree, or notes commits that fast-forward origin/main:
#     worktree:update rebases <name> onto master (resolving notes-gitlink
#     conflicts) and master FFs to it. Notes are FF-pushed, so no merge commit
#     and no SHA change -> the gitlinks the parent already records stay valid.
#   - origin/main has DIVERGED past master:notes (a concurrent worktree merged
#     notes meanwhile): the notes branch can't FF, so merge it into origin/main
#     (--no-ff) and pin master's gitlink to that merge commit via a --no-ff
#     parent merge (--no-commit + force notes + add, so no throwaway bump commit).
#     A merge commit appears only here -- when a merge genuinely happened.
# Conflicts (the notes rebase, the notes merge, or code) halt with set -e for
# manual resolution; rm doesn't run. Operates from the main worktree so
# worktree:rm removing the caller's cwd can't strand the script.
set -euo pipefail

repo=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
cd "$repo"
wt=".worktrees/$usage_name"
[ -d "$wt" ] || {
  echo "worktree:merge: $wt not found" >&2
  exit 1
}
main_branch=$(git rev-parse --abbrev-ref HEAD)
[ "$main_branch" = "master" ] || {
  echo "worktree:merge: main worktree on '$main_branch', expected master" >&2
  exit 1
}

# The commit master currently records for the notes submodule. The worktree's
# notes branch was forked from this when the worktree was created; rebasing onto
# its current value (below) re-bases any stale branch onto what master points at
# now, before integrating. Empty when the repo has no notes submodule.
master_notes=$(git rev-parse -q --verify master:notes 2>/dev/null || true)

# Classify the worktree's notes branch (after rebasing it onto master:notes):
# none | ff (linear ahead of origin/main) | merge (origin/main advanced past
# master:notes, so diverged). Detached/agent worktrees have no <name> notes branch.
wt_notes="$repo/$wt/notes"
notes_action="none"
if [ -n "$master_notes" ] && [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  git -C "$wt_notes" fetch -q origin
  # master:notes must be reachable in this worktree's own notes clone to rebase
  # onto it -- clones exchange objects via origin (where it normally lives) or a
  # direct fetch from the main checkout's notes, not a shared object store.
  git -C "$wt_notes" fetch -q "$repo/notes" "$master_notes" 2>/dev/null || true
  git -C "$wt_notes" rebase "$master_notes" "$usage_name"
  if [ "$(git -C "$wt_notes" rev-list --count "$master_notes".."$usage_name")" -gt 0 ]; then
    if git -C "$wt_notes" merge-base --is-ancestor origin/main "$usage_name"; then
      notes_action="ff"
    else
      notes_action="merge"
    fi
  fi
fi

if [ "$notes_action" = "merge" ]; then
  # notes/main diverged: merge the worktree's notes branch into it (push), then
  # pin master's gitlink to that merge commit inside one --no-ff merge commit.
  git -C "$wt_notes" checkout -q -B main origin/main
  git -C "$wt_notes" merge --no-ff -m "notes: merge $usage_name" "$usage_name"
  notes_tip=$(git -C "$wt_notes" rev-parse main)
  git -C "$wt_notes" push -q origin main
  git -C notes fetch -q origin # make notes_tip reachable for the parent merge
  git -c advice.submoduleMergeConflict=false merge --no-ff --no-commit "$usage_name" || true
  git -C notes checkout -q "$notes_tip"
  git add notes
  if git diff --name-only --diff-filter=U | grep -q .; then
    echo "worktree:merge: conflicts merging '$usage_name' into master." >&2
    echo "  Resolve in $repo, 'git commit', then 'mise run worktree:rm $usage_name'." >&2
    exit 1
  fi
  git commit -q --no-edit -m "notes: Update pointer"
else
  # linear path: delegate the rebase to worktree:update -- it rebases the notes
  # branch onto master:notes (a no-op here, already done above) and the code
  # branch onto master, resolving the notes-gitlink conflicts that a bare
  # `git rebase master` would halt on once the notes branch has been rewritten.
  # Then FF master to it. For notes_action=ff, FF-push the notes branch (SHAs
  # preserved, so the rebased gitlinks stay valid).
  mise run worktree:update "$usage_name"
  if [ "$notes_action" = "ff" ]; then
    git -C "$wt_notes" push -q origin "$usage_name":main
    git -C notes fetch -q origin
  fi
  git merge --ff-only "$usage_name"
fi

git submodule update --init notes

# submodule update checks the recorded gitlink out as a detached HEAD; put notes
# back on its main branch, fast-forwarded to that gitlink, so notes edits in the
# main checkout commit onto main (which the next wt:merge integrates) instead of
# stranding on a detached HEAD. Both integration paths above pushed the target to
# origin/main and it equals the gitlink, so main fast-forwards cleanly; --ff-only
# halts (set -e) on a diverged local main rather than silently resetting it.
if [ -n "$master_notes" ]; then
  notes_ptr=$(git rev-parse HEAD:notes)
  git -C notes checkout -q main
  git -C notes merge --ff-only "$notes_ptr"
fi

mise run worktree:rm "$usage_name"

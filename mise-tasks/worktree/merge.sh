#!/usr/bin/env bash
#MISE description="Merge <name> into master: FF when possible, --no-ff only when the notes submodule diverged; then worktree:rm <name>"
#MISE alias="wt:merge"
#USAGE arg "<name>" help="Branch and worktree name"
# shellcheck disable=SC2154  # usage_name injected by mise from the #USAGE spec

# Integrates the worktree branch <name> into master, keeping history linear
# whenever it safely can:
#   - code-only worktree, or notes commits that fast-forward notes/main: rebase
#     <name> onto master and FF. Notes are FF-pushed, so no merge commit and no
#     SHA change -> the gitlinks the parent already records stay valid as-is.
#   - notes/main has DIVERGED (a concurrent worktree merged notes meanwhile):
#     the notes branch can't FF, so merge it into notes/main (--no-ff) and pin
#     master's gitlink to that merge commit via a --no-ff parent merge
#     (--no-commit + force notes + add, so no throwaway bump commit). A merge
#     commit appears only here -- when a merge genuinely happened.
# Conflicts (notes-side or code) halt with set -e for manual resolution; rm
# doesn't run. Operates from the main worktree so worktree:rm removing the
# caller's cwd can't strand the script.
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

# Classify the worktree's notes branch: none | ff (strictly ahead of notes/main)
# | merge (diverged). Detached/agent worktrees have no <name> notes branch.
wt_notes="$repo/$wt/notes"
notes_action="none"
if [ -d "$wt_notes" ] && git -C "$wt_notes" rev-parse --verify -q "$usage_name" >/dev/null; then
  git -C "$wt_notes" fetch -q origin
  if [ "$(git -C "$wt_notes" rev-list --count origin/main.."$usage_name")" -gt 0 ]; then
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
  git commit -q --no-edit
else
  # linear path: rebase onto master, FF. For notes_action=ff, FF-push the notes
  # branch first (SHAs preserved, so the rebased parent's gitlinks stay valid).
  git -C "$wt" rebase master
  if [ "$notes_action" = "ff" ]; then
    git -C "$wt_notes" push -q origin "$usage_name":main
    git -C notes fetch -q origin
  fi
  git merge --ff-only "$usage_name"
fi

git submodule update --init notes
mise run worktree:rm "$usage_name"

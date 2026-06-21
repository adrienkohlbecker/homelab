#!/usr/bin/env bash

worktree_task="worktree:${0##*/}"
worktree_task="${worktree_task%.sh}"

main_worktree() {
  git worktree list --porcelain | awk '/^worktree / {print substr($0, 10); exit}'
}

resolve_side_worktree() {
  local repo=$1 requested=$2 abs="" wt=""

  if [ -d "$requested" ]; then
    abs=$(git -C "$requested" rev-parse --show-toplevel 2>/dev/null || true)
  fi

  wt=$(git -C "$repo" worktree list --porcelain | awk -v abs="$abs" -v b="refs/heads/$requested" '
    /^worktree / {p = substr($0, 10)}
    abs != "" && p == abs {print p; exit}
    $0 == "branch " b {print p; exit}')
  [ -n "$wt" ] || {
    echo "${worktree_task}: no worktree found for '$requested'" >&2
    return 1
  }
  [ "$wt" != "$repo" ] || {
    echo "${worktree_task}: refusing to operate on the main worktree" >&2
    return 1
  }

  printf '%s\n' "$wt"
}

require_main_master() {
  local repo=$1 main_branch

  main_branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD)
  [ "$main_branch" = master ] || {
    echo "${worktree_task}: main worktree on '$main_branch', expected master" >&2
    return 1
  }
}

remove_clean_worktree() {
  local repo=$1 wt=$2 branch=""

  branch=$(git -C "$wt" symbolic-ref --quiet --short HEAD || true)
  if [ -n "$(git -C "$wt" status --porcelain)" ]; then
    echo "${worktree_task}: $wt has uncommitted changes; commit/stash or remove it manually" >&2
    return 1
  fi

  git -C "$repo" worktree remove --force "$wt"
  if [ -n "$branch" ]; then
    git -C "$repo" branch -D "$branch" 2>/dev/null || true
  fi
}

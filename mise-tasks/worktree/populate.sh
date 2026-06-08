#!/usr/bin/env bash
#MISE description="Populate a freshly-created worktree: packer/artifacts symlink, .worktreeinclude copies, mise trust"
#MISE alias="wt:populate"
#USAGE arg "<worktree>" help="Absolute path to the new worktree"
# shellcheck disable=SC2154  # usage_worktree injected by mise from the #USAGE spec
set -euo pipefail

# Accept the worktree as positional ($1) so the script is callable
# without mise on PATH (the WorktreeCreate hook does this).
wt="${1:-${usage_worktree:?Usage: $(basename "$0") <worktree-path>}}"
wt=$(cd "$wt" && pwd)

# Main repo = first entry in `worktree list` (always the real one).
repo=$(git -C "$wt" worktree list --porcelain | awk '/^worktree / {print $2; exit}')

if [ ! -e "$wt/packer/artifacts" ] && [ ! -L "$wt/packer/artifacts" ]; then
  ln -s "$repo/packer/artifacts" "$wt/packer/artifacts"
fi

if [ ! -e "$wt/terraform/.terraform" ] && [ ! -L "$wt/terraform/.terraform" ]; then
  ln -s "$repo/terraform/.terraform" "$wt/terraform/.terraform"
fi

if [ ! -e "$wt/.claude/settings.local.json" ] && [ ! -L "$wt/.claude/settings.local.json" ]; then
  ln -s "$repo/.claude/settings.local.json" "$wt/.claude/settings.local.json"
fi

# remember-plugin memory store: the plugin hardcodes $CLAUDE_PROJECT_DIR/.remember,
# so a per-worktree store dies with `git worktree remove`. Share the main checkout's
# (ignored via the common git dir's info/exclude). Guard on the source existing — a
# dangling symlink would break the plugin's own `mkdir -p .remember/tmp`.
if [ -d "$repo/.remember" ] && [ ! -e "$wt/.remember" ] && [ ! -L "$wt/.remember" ]; then
  ln -s "$repo/.remember" "$wt/.remember"
fi

if [ -f "$repo/.worktreeinclude" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | '#'*) continue ;; esac
    src="$repo/$line"
    dst="$wt/$line"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
      continue
    fi
    if [ ! -e "$src" ] && [ ! -L "$src" ]; then
      echo "worktree:populate: skipping '$line' (not present in $repo)" >&2
      continue
    fi
    mkdir -p "$(dirname "$dst")"
    cp -RP "$src" "$dst"
  done <"$repo/.worktreeinclude"
fi

if command -v mise >/dev/null; then
  mise trust "$wt/mise.toml"
fi

if command -v uv >/dev/null && [ -f "$wt/pyproject.toml" ]; then
  uv sync --project "$wt" --quiet
fi

# notes submodule: register as a linked worktree of the main notes checkout so
# `git -C notes worktree list` enumerates all active worktrees.  Put it on a
# branch matching the parent worktree's branch so notes edits commit onto
# <branch> (wt:merge integrates back into notes/main).  Non-fatal: offline or
# missing main notes checkout yields a usable code worktree; a missing notes
# branch just means wt:merge skips the notes step.
# Idempotent: skip if already registered, or if the path exists via an old-style
# submodule clone (different gitdir path — migrate by removing and re-running).
if git -C "$wt" config -f "$wt/.gitmodules" --get submodule.notes.path >/dev/null 2>&1 &&
   { [ -d "$repo/notes" ] && git -C "$repo/notes" rev-parse --git-dir >/dev/null 2>&1; }; then
  if git -C "$repo/notes" worktree list --porcelain 2>/dev/null | grep -qxF "worktree $wt/notes"; then
    : # already registered as a worktree — nothing to do
  elif [ -e "$wt/notes/.git" ]; then
    : # path exists via old-style submodule clone — leave it alone
  else
    branch=$(git -C "$wt" symbolic-ref --short -q HEAD || true)
    gitlink=$(git -C "$wt" rev-parse "HEAD:notes" 2>/dev/null || true)
    if [ -n "$branch" ] && [ "$branch" != master ]; then
      if git -C "$repo/notes" rev-parse --verify -q "refs/heads/$branch" >/dev/null 2>&1; then
        # Branch exists — --force allows reusing a branch already checked out in
        # another worktree (e.g. two sessions working the same task branch)
        git -C "$repo/notes" worktree add --force "$wt/notes" "$branch" >/dev/null ||
          echo "worktree:populate: notes worktree add failed" >&2
      else
        # Branch doesn't exist — create it at the gitlink commit so notes starts
        # at the same recorded submodule pointer as the parent worktree
        git -C "$repo/notes" worktree add -b "$branch" "$wt/notes" ${gitlink:+"$gitlink"} >/dev/null ||
          echo "worktree:populate: notes worktree add failed" >&2
      fi
    else
      # master / detached parent: detach notes at the gitlink commit
      git -C "$repo/notes" worktree add --detach "$wt/notes" ${gitlink:+"$gitlink"} >/dev/null ||
        echo "worktree:populate: notes worktree add failed" >&2
    fi
  fi
fi

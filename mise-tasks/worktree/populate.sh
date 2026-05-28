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

# notes submodule: populate it and put it on a branch matching the parent
# worktree's branch, so notes edits here commit onto <branch> (which wt:merge
# later integrates into notes/main). A detached worktree (agent isolation, where
# new==base) has no parent branch -> notes stays detached at the recorded SHA.
# Non-fatal: an offline create still yields a usable code worktree, and a
# missing notes branch just means wt:merge skips the notes step.
if git -C "$wt" config -f "$wt/.gitmodules" --get submodule.notes.path >/dev/null 2>&1; then
  if git -C "$wt" submodule update --init notes >/dev/null 2>&1; then
    branch=$(git -C "$wt" symbolic-ref --short -q HEAD || true)
    if [ -n "$branch" ] && [ "$branch" != master ] &&
      ! git -C "$wt/notes" rev-parse --verify -q "$branch" >/dev/null; then
      git -C "$wt/notes" switch -c "$branch" >/dev/null
    fi
  else
    echo "worktree:populate: notes submodule init skipped (offline?)" >&2
  fi
fi

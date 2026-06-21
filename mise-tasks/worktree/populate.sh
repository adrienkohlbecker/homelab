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

symlink_missing() {
  local src=$1 dst=$2

  if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
    ln -s "$src" "$dst"
  fi
}

symlink_existing_dir() {
  local src=$1 dst=$2

  if [ -d "$src" ]; then
    symlink_missing "$src" "$dst"
  fi
}

symlink_missing "$repo/packer/artifacts" "$wt/packer/artifacts"
symlink_missing "$repo/terraform/.terraform" "$wt/terraform/.terraform"
symlink_missing "$repo/.claude/settings.local.json" "$wt/.claude/settings.local.json"

# test/firmware/ holds the fetched aarch64 edk2 blob (gitignored; see test/arch.py).
# Share the main checkout's so one `mise run test:firmware` covers every worktree.
# Guarded on the source existing so we never leave a dangling symlink that arch.py
# would misread -- a worktree created before the first fetch just fetches its own.
symlink_existing_dir "$repo/test/firmware" "$wt/test/firmware"

# remember-plugin memory store: the plugin hardcodes $CLAUDE_PROJECT_DIR/.remember,
# so a per-worktree store dies with `git worktree remove`. Share the main checkout's
# (ignored via the common git dir's info/exclude). Guard on the source existing — a
# dangling symlink would break the plugin's own `mkdir -p .remember/tmp`.
symlink_existing_dir "$repo/.remember" "$wt/.remember"

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

# notes/ is a single shared clone the main checkout owns (gitignored; see the repo
# .gitignore). Every worktree symlinks to it, so notes written from any worktree
# land on the one notes history -- no per-worktree clone, branch, or merge. Matches
# the packer/artifacts and .remember symlinks above. Skipped when the main checkout
# has no notes clone (fresh setup, or a CI checkout that never populated it).
if [ -d "$repo/notes/.git" ]; then
  symlink_missing "$repo/notes" "$wt/notes"
fi

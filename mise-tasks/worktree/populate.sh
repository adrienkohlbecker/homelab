#!/usr/bin/env bash
#MISE description="Populate a freshly-created worktree: shared state, local config, mise trust"
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

# Codex creates managed worktrees with a detached HEAD. Give each one a stable,
# repository-visible branch named after its unique worktree directory, matching
# the branch-backed worktrees created by the WorktreeCreate hook.
if [ -n "${CODEX_WORKTREE_PATH:-}" ] && ! git -C "$wt" symbolic-ref --quiet HEAD >/dev/null; then
  codex_wt=$(cd "$CODEX_WORKTREE_PATH" && pwd)
  [ "$codex_wt" = "$wt" ] || {
    echo "worktree:populate: CODEX_WORKTREE_PATH does not match '$wt'" >&2
    exit 1
  }

  codex_branch="codex/$(basename "$(dirname "$wt")")"
  codex_branch_ref="refs/heads/$codex_branch"
  if codex_branch_head=$(git -C "$repo" rev-parse --verify "$codex_branch_ref" 2>/dev/null); then
    [ "$codex_branch_head" = "$(git -C "$wt" rev-parse HEAD)" ] || {
      echo "worktree:populate: branch '$codex_branch' already points at another commit" >&2
      exit 1
    }
    git -C "$wt" switch "$codex_branch"
  else
    git -C "$wt" switch -c "$codex_branch"
  fi
fi

symlink_missing() {
  local path=$1

  if [ ! -e "$wt/$path" ] && [ ! -L "$wt/$path" ]; then
    ln -s "$repo/$path" "$wt/$path"
  fi
}

symlink_missing packer/artifacts
symlink_missing terraform/.terraform

for copied_path in .ansible-mitogen-strategy mise.local.toml; do
  src="$repo/$copied_path"
  dst="$wt/$copied_path"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    continue
  fi
  if [ ! -e "$src" ] && [ ! -L "$src" ]; then
    echo "worktree:populate: skipping '$copied_path' (not present in $repo)" >&2
    continue
  fi
  cp -RP "$src" "$dst"
done

if command -v mise >/dev/null; then
  mise trust "$wt/mise.toml"
fi

if command -v uv >/dev/null && [ -f "$wt/pyproject.toml" ]; then
  uv sync --project "$wt" --quiet
fi

# notes/ is a single shared clone the main checkout owns (gitignored; see the repo
# .gitignore). Every worktree symlinks to it, so notes written from any worktree
# land on the one notes history -- no per-worktree clone, branch, or merge. Skipped
# when the main checkout has no notes clone (fresh setup, or a CI checkout that
# never populated it).
if [ -d "$repo/notes/.git" ]; then
  symlink_missing notes
fi

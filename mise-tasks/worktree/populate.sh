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

# shellcheck source=mise-tasks/worktree/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

repo=$(main_worktree "$wt")

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

# notes/ links to the private notes repository (dotfiles project_notes). The
# global post-checkout hook links plain-git and Claude Code worktrees, but the
# Codex app runs git with core.hooksPath=/dev/null, so link explicitly here.
# A no-op when already linked, and skipped where project_notes isn't installed
# (~/.local/bin is spelled out: the GUI-launched app may lack it on PATH).
# Best-effort, like the hook: a notes problem must not fail worktree setup.
notes_cli=$(command -v project_notes || echo "$HOME/.local/bin/project_notes")
if [ -x "$notes_cli" ]; then
  "$notes_cli" link --repo "$wt" || echo "worktree:populate: project_notes link failed; notes/ not linked" >&2
fi

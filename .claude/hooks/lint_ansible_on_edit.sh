#!/usr/bin/env bash
# Claude Code PostToolUse hook: runs `mise run lint:ansible-changed` after
# Edit/Write/MultiEdit on *.yml / *.yaml. Non-blocking -- the edit always
# succeeds; on lint failure the output is fed back to Claude so it sees the
# regression at edit-time instead of waiting for the next manual run.
set -euo pipefail

input=$(cat)
file=$(jq -r '.tool_input.file_path // empty' <<<"$input")

case "$file" in
*.yml | *.yaml) ;;
*) exit 0 ;;
esac

[ -f "$file" ] || exit 0

# Use the file's own repo root rather than $CLAUDE_PROJECT_DIR -- worktrees
# share .git/refs/ but `lint:ansible-changed` must run in the worktree whose
# diff actually contains the just-edited file.
root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

if out=$(mise run lint:ansible-changed 2>&1); then
  exit 0
fi

printf 'ansible-lint failed (%s):\n%s\n' "$root" "$out" >&2
exit 0

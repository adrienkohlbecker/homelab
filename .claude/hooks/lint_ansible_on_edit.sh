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

# Confine to the project repo and its worktrees: a worktree shares the project's
# git common dir, an unrelated repo does not. Without this, editing a file that
# lives in a hostile clone would run that clone's mise.toml `lint:ansible-changed`
# task as the operator.
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  proj=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  here=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  [ "$proj" = "$here" ] || exit 0
fi

cd "$root" || exit 0

if out=$(mise run lint:ansible-changed 2>&1); then
  exit 0
fi

# Feed the failure back as additionalContext (guaranteed into Claude's context,
# wrapped in a system reminder) rather than bare stderr. Non-blocking: the edit
# already succeeded.
msg=$(printf 'ansible-lint failed (%s):\n%s' "$root" "$out")
jq -n --arg ctx "$msg" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
exit 0

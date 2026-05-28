#!/usr/bin/env bash
# Claude Code PostToolUse hook: runs `tofu validate` after Edit/Write/MultiEdit
# on *.tf / *.tfvars. Goes through `mise exec --` rather than the `mise run tf`
# wrapper so we skip `op run`'s 1Password resolve -- validate is offline-only
# and doesn't read any of the op:// env refs. Non-blocking; surfaces output to
# Claude on failure only.
set -euo pipefail

input=$(cat)
file=$(jq -r '.tool_input.file_path // empty' <<<"$input")

case "$file" in
*.tf | *.tfvars) ;;
*) exit 0 ;;
esac

[ -f "$file" ] || exit 0

root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Confine to the project repo and its worktrees: a worktree shares the project's
# git common dir, an unrelated repo does not. Without this, editing a .tf that
# lives in a hostile clone would run that clone's mise/tofu (and load its
# provider plugins from .terraform/) as the operator.
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  proj=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  here=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  [ "$proj" = "$here" ] || exit 0
fi

tf="$root/terraform"

# .terraform/ is gitignored -- skip silently in fresh worktrees that haven't
# been `tofu init`'d yet rather than spamming "Could not load plugin" errors
# every time someone touches a .tf file in a freshly-created worktree.
[ -d "$tf/.terraform" ] || exit 0

cd "$tf" || exit 0

if out=$(mise exec -- tofu validate 2>&1); then
  exit 0
fi

# Feed the failure back as additionalContext (guaranteed into Claude's context,
# wrapped in a system reminder) rather than bare stderr. Non-blocking: the edit
# already succeeded.
msg=$(printf 'tofu validate failed (%s):\n%s' "$tf" "$out")
jq -n --arg ctx "$msg" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
exit 0

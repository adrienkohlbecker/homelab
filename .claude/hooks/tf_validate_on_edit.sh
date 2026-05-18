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
tf="$root/terraform"

# .terraform/ is gitignored -- skip silently in fresh worktrees that haven't
# been `tofu init`'d yet rather than spamming "Could not load plugin" errors
# every time someone touches a .tf file in a freshly-created worktree.
[ -d "$tf/.terraform" ] || exit 0

cd "$tf" || exit 0

if out=$(mise exec -- tofu validate 2>&1); then
  exit 0
fi

printf 'tofu validate failed (%s):\n%s\n' "$tf" "$out" >&2
exit 0

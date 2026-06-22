#!/usr/bin/env bash
# Agent PostToolUse hook: validate edited Ansible/Terraform files and return
# failures as additional context without blocking the edit.
set -euo pipefail

input=$(cat)
file=$(jq -r '.tool_input.file_path // empty' <<<"$input")

case "$file" in
*.yml | *.yaml)
  label=ansible-lint
  ;;
*.tf | *.tfvars)
  label="tofu validate"
  ;;
*)
  exit 0
  ;;
esac

[ -f "$file" ] || exit 0

root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Keep project hooks confined to this repo and its worktrees.
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  proj=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  here=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  [ "$proj" = "$here" ] || exit 0
fi

if [ "$label" = ansible-lint ]; then
  workdir=$root
  cmd=(mise run lint:ansible-changed)
else
  workdir="$root/terraform"
  # Fresh worktrees may not have provider plugins yet.
  [ -d "$workdir/.terraform" ] || exit 0
  cmd=(mise exec -- tofu validate)
fi

cd "$workdir" || exit 0

if out=$("${cmd[@]}" 2>&1); then
  exit 0
fi

msg=$(printf '%s failed (%s):\n%s' "$label" "$workdir" "$out")
jq -n --arg ctx "$msg" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
exit 0

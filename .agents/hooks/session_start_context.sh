#!/usr/bin/env bash
# Claude Code SessionStart hook: surface two homelab invariants up front (as
# additionalContext) instead of letting a task discover them the hard way
# mid-flight -- whether the notes/ submodule is checked out, and whether the
# 1Password CLI is signed in (the op:// env refs in mise.toml only resolve when
# it is).
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"

notes="notes/ submodule: n/a"
if [ -f "$root/.gitmodules" ] && grep -q 'path = notes' "$root/.gitmodules" 2>/dev/null; then
  if [ -e "$root/notes/.git" ]; then
    notes="notes/ submodule: checked out"
  else
    notes="notes/ submodule: NOT initialised (git submodule update --init notes)"
  fi
fi

# stdin from /dev/null so a not-signed-in op can never block on an interactive
# unlock prompt; whoami is account-status only, not item access.
if ! command -v op >/dev/null 2>&1; then
  op_state="1Password CLI: not installed"
elif op whoami >/dev/null 2>&1 </dev/null; then
  op_state="1Password CLI: signed in"
else
  op_state="1Password CLI: NOT signed in (op:// refs in mise.toml will not resolve)"
fi

ctx=$(printf '%s\n%s' "$notes" "$op_state")
jq -n --arg ctx "$ctx" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'

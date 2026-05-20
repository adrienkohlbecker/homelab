#!/usr/bin/env bash
# Claude Code PreToolUse hook: blocks `git push` on ansible-lint regression.
#
# The PostToolUse counterpart (lint_ansible_on_edit.sh) is non-blocking --
# it feeds lint output back to Claude after each edit so the regression is
# visible during the dev loop. This hook is the gate: by the time a push
# happens, lint had better be clean. Exit 2 surfaces stderr to Claude and
# blocks the tool execution.
#
# CLAUDE.md ("Build, Test, and Development Commands") says "Run full
# `mise run lint` before pushing"; this hook makes that machine-enforced.
set -euo pipefail

input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")

# Only act on `git push` invocations -- not `git push --help`, `git status`,
# or anything else routed through Bash. Use a token-level match rather than
# substring so `git push-pr-helper`-style false positives don't trigger.
case "$cmd" in
  "git push"|"git push "*) ;;
  *) exit 0 ;;
esac

[ -n "$cwd" ] || cwd=$(pwd)
root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

if out=$(mise run lint:ansible-changed 2>&1); then
  exit 0
fi

# shellcheck disable=SC2016  # the `mise run lint` literal is intentional message text, not an expression
printf 'ansible-lint failed -- push blocked. Run `mise run lint` to reproduce locally.\n%s\n' "$out" >&2
exit 2

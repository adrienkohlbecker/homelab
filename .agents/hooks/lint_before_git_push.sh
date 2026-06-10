#!/usr/bin/env bash
# Claude Code PreToolUse hook: blocks `git push` on a `mise run lint` regression.
#
# The PostToolUse counterpart (lint_ansible_on_edit.sh) is non-blocking and runs
# only the fast changed-ansible variant after each edit so the regression is
# visible during the dev loop. This hook is the gate: by the time a push happens,
# the full lint had better be clean. Exit 2 surfaces stderr to Claude and blocks
# the tool execution.
#
# CLAUDE.md ("Build, Test, and Development Commands") says "Run full
# `mise run lint` before pushing"; this hook makes that machine-enforced.
set -euo pipefail

input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")

# Only act on `git push` invocations -- not `git push --help`, `git status`,
# or anything else routed through Bash. Token-level match (not substring) so
# `git push-pr-helper`-style false positives don't trigger; the `git -C <dir>`
# forms are caught too since the repo's worktree flows push with `git -C`.
case "$cmd" in
"git push" | "git push "*) ;;
"git -C "*" push" | "git -C "*" push "*) ;;
*) exit 0 ;;
esac

[ -n "$cwd" ] || cwd=$(pwd)
root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Confine to the project repo and its worktrees (shared git common dir) so a
# push from an unrelated hostile clone can't run that clone's `mise run lint`.
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  proj=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  here=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  [ "$proj" = "$here" ] || exit 0
fi

cd "$root" || exit 0

# Full lint (not the changed-only inner-loop variant): the push gate is the last
# checkpoint, and CLAUDE.md requires a clean full `mise run lint` before pushing
# -- the changed variant skips tofu/shell/yaml and would pass a broken .tf.
if out=$(mise run lint 2>&1); then
  exit 0
fi

# shellcheck disable=SC2016  # the `mise run lint` literal is intentional message text, not an expression
printf 'lint failed -- push blocked. Run `mise run lint` to reproduce locally.\n%s\n' "$out" >&2
exit 2

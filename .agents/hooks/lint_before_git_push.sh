#!/usr/bin/env bash
# Agent PreToolUse hook: blocks `git push` on a `mise run lint` regression.
#
# The PostToolUse counterpart is non-blocking and runs focused validation during
# the edit loop. This hook is the gate: by the time a push happens, the full
# lint had better be clean. Exit 2 surfaces stderr to the agent and blocks the
# tool execution.
#
# AGENTS.md ("Build, Test, and Development Commands") says "Run full
# `mise run lint` before pushing"; this hook makes that machine-enforced.
set -euo pipefail

input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")

# Drop heredoc bodies before scanning. A commit fed via `git commit -F - <<EOF
# ... EOF` can legitimately contain a line that reads like a `git push ...`
# command (e.g. a message documenting this very flag) -- that is data, not an
# invocation, and must not trip the gate. Track heredoc openers (`<<WORD`,
# `<<'WORD'`, `<<-WORD`) and drop everything through the terminator line. The
# opener guard `[^<[:alnum:]]` before `<<` skips `<<<` herestrings and `a<<b`
# arithmetic shifts; a bare `cmd<<EOF` with no space is not recognised (we only
# emit `<<'EOF'`-with-space ourselves).
strip_heredocs() {
  local line probe out="" delim="" dash="" in_hd=0
  local opener_re='(^|[^<[:alnum:]])<<(-?)[[:space:]]*['\''"]?([A-Za-z_][A-Za-z0-9_]*)'
  while IFS= read -r line; do
    if [ "$in_hd" = 1 ]; then
      probe=$line
      # `<<-` lets the terminator be indented with leading tabs.
      [ -n "$dash" ] && probe=${probe#"${probe%%[!$'\t']*}"}
      [ "$probe" = "$delim" ] && in_hd=0
      continue
    fi
    out+=$line$'\n'
    if [[ $line =~ $opener_re ]]; then
      dash=${BASH_REMATCH[2]}
      delim=${BASH_REMATCH[3]}
      in_hd=1
    fi
  done <<<"$1"
  printf '%s' "$out"
}
cmd=$(strip_heredocs "$cmd")

# Only act on `git push` invocations -- not `git push --help`, `git status`,
# or anything else routed through Bash. Check individual shell segments so a
# chained command such as `git status && git push` still gets gated.
normalized=${cmd//$'\n'/;}
normalized=${normalized//&&/$'\n'}
normalized=${normalized//||/$'\n'}
normalized=${normalized//;/$'\n'}

needs_lint=false
while IFS= read -r segment; do
  segment=${segment#"${segment%%[![:space:]]*}"}
  case "$segment" in
  "git push --help" | "git push -h" | "git push help" | "git -C "*" push --help" | "git -C "*" push -h" | "git -C "*" push help")
    continue
    ;;
  "git push" | "git push "* | "git -C "*" push" | "git -C "*" push "*)
    needs_lint=true
    break
    ;;
  esac
done <<<"$normalized"

[ "$needs_lint" = true ] || exit 0

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
# checkpoint, and AGENTS.md requires a clean full `mise run lint` before pushing
# -- the changed variant skips tofu/shell/yaml and would pass a broken .tf.
if out=$(mise run lint 2>&1); then
  exit 0
fi

# shellcheck disable=SC2016  # the `mise run lint` literal is intentional message text, not an expression
printf 'lint failed -- push blocked. Run `mise run lint` to reproduce locally.\n%s\n' "$out" >&2
exit 2

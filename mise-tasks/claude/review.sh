#!/usr/bin/env bash
#MISE description="Ask Claude to review an explicit target, worktree diff, or master changes"
#MISE alias="claude-review"
#USAGE arg "[target]..." help="Optional pathspec, commit, or revision range"
set -euo pipefail

agent_label="claude-review"
base_ref="${CLAUDE_REVIEW_BASE:-master}"
dry_run="${CLAUDE_REVIEW_DRY_RUN:-}"

# claude -p buffers the entire turn in text mode, so a multi-minute verifying
# review prints nothing until it completes and looks frozen. Render the
# stream-json event feed instead so tool activity and findings show up live.
stream_review() {
  jq --unbuffered -rj '
    def arg: (.input // {})
      | (.file_path // .path // .pattern // .command // .url // .query // .description // "")
      | tostring | .[0:120];
    if .type == "assistant" then
      ( .message.content[]?
        | if .type == "text" then .text
          elif .type == "tool_use" then "\n  · " + .name + ": " + arg + "\n"
          else "" end )
    elif .type == "result" then
      ( if (.subtype // "success") == "success" then "\n"
        else "\n[claude-review] stream ended: " + (.subtype // "unknown") + "\n" end )
    else empty end
  '
}

run_agent() {
  local claude_args

  if ! command -v claude >/dev/null 2>&1; then
    echo "claude-review: claude command not found" >&2
    exit 127
  fi

  # dontAsk runs read-only Bash (git log/blame/show) plus the allow-listed tools
  # without prompting, and auto-denies anything that would mutate the tree, so
  # the reviewer can verify claims against the live checkout but cannot edit it.
  claude_args=(
    -p
    --permission-mode dontAsk
    --allowed-tools "Read,Grep,Glob,Bash,WebFetch,WebSearch"
    --disallowed-tools "Edit,Write,NotebookEdit"
  )
  # Verification reads many files, so a review already runs for minutes. Opt into
  # deeper reasoning with CLAUDE_REVIEW_EFFORT=high; unset keeps the session default.
  if [ -n "${CLAUDE_REVIEW_EFFORT:-}" ]; then
    claude_args+=(--effort "$CLAUDE_REVIEW_EFFORT")
  fi
  if [ -n "${CLAUDE_REVIEW_MODEL:-}" ]; then
    claude_args+=(--model "$CLAUDE_REVIEW_MODEL")
  fi

  echo "claude-review: $scope (findings stream as the agent works)" >&2

  if command -v jq >/dev/null 2>&1; then
    claude "${claude_args[@]}" --output-format stream-json --verbose <"$prompt_file" | stream_review
  else
    claude "${claude_args[@]}" <"$prompt_file"
  fi
}

script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../lib/review_common.sh
source "$script_dir/../lib/review_common.sh"

review_main "$@"

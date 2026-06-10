#!/usr/bin/env bash
#MISE description="Ask Codex to review an explicit target, worktree diff, or master changes"
#MISE alias="codex-review"
#USAGE arg "[target]..." help="Optional pathspec, commit, or revision range"
set -euo pipefail

agent_label="codex-review"
base_ref="${CODEX_REVIEW_BASE:-master}"
dry_run="${CODEX_REVIEW_DRY_RUN:-}"

run_agent() {
  local codex_args

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex-review: codex command not found" >&2
    exit 127
  fi

  codex_args=(
    exec
    --sandbox read-only
    --cd "$repo"
  )
  if [ -n "${CODEX_REVIEW_MODEL:-}" ]; then
    codex_args+=(--model "$CODEX_REVIEW_MODEL")
  fi
  if [ -n "${CODEX_REVIEW_PROFILE:-}" ]; then
    codex_args+=(--profile "$CODEX_REVIEW_PROFILE")
  fi

  echo "codex-review: $scope" >&2
  codex "${codex_args[@]}" - <"$prompt_file"
}

script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../lib/review_common.sh
source "$script_dir/../lib/review_common.sh"

review_main "$@"

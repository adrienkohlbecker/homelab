#!/usr/bin/env bash
set -euo pipefail

colorize_stderr() {
  while IFS= read -r line; do
    if [[ $line == "+"* ]]; then
      printf '\e[0;30m%s\e[0m\n' "$line" >&2
    else
      printf '\e[0;41m%s\e[0m\n' "$line" >&2
    fi
  done
}

script=$1
shift

args=("$@")
role="${args[-1]}"
machine="${args[-2]}"

(
  "$script" --checkmode "${args[@]}" 2> >(colorize_stderr)
) &>"$OUT_DIR/$role.$machine.ansi" || {
  printf '\e[0;41m%s failed\e[0m\n' "$role.$machine" >&2
  exit 1
}

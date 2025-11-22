#!/usr/bin/env bash
set -euo pipefail

export OUT_DIR="test/out"
export LOG_FILE="test/out.log"

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.ansi

colorize_stderr() {
  # ansible --check emits + for ok/changed lines; anything else is treated as error-ish
  while IFS= read -r line; do
    if [[ $line == "+"* ]]; then
      printf '\e[0;30m%s\e[0m\n' "$line" >&2
    else
      printf '\e[0;41m%s\e[0m\n' "$line" >&2
    fi
  done
}
export -f colorize_stderr

run_role() {
  local script=$1
  local role=$2
  shift 2

  (
    "$script" "$role" --checkmode "$@" 2> >(colorize_stderr)
  ) &>"$OUT_DIR/$role.ansi" || {
    printf '\e[0;41m%s failed\e[0m\n' "$role" >&2
    exit 1
  }
}
export -f run_role

[ ! -f "$LOG_FILE" ] || cp "$LOG_FILE" "$LOG_FILE.prev"

list_roles() {
  for role_dir in roles/*; do
    [[ -d $role_dir && -f $role_dir/tasks/main.yml ]] && basename "$role_dir"
  done | sort
}

PARALLEL=(parallel --jobs 5 --joblog "$LOG_FILE" --eta run_role test/testrole.sh)
if [[ ${1:-} == "--onlyfailed" ]]; then
  shift
  test/showfailed.sh | "${PARALLEL[@]}" "$@"
else
  mapfile -t roles < <(list_roles)
  if ((${#roles[@]} == 0)); then
    echo "No roles with tasks/main.yml found" >&2
    exit 1
  fi
  printf '%s\n' "${roles[@]}" | "${PARALLEL[@]}" "$@"
fi

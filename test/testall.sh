#!/usr/bin/env bash
set -euo pipefail

export OUT_DIR="test/out"
export LOG_FILE="test/out.log"

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.ansi

ONLY_FAILED=0
JOBS=5
ROLE_ARGS=()
MACHINES=(container)
PARALLEL_PID=""

# Flags:
#   --onlyfailed : rerun only roles that failed in the last log
#   --jobs N     : number of parallel workers (default: 5)
#   --machines X : comma-separated list of machine profiles (default: container)
#   --           : stop parsing and forward remaining args to testrole.sh
while [[ $# -gt 0 ]]; do
  case "$1" in
    --onlyfailed)
      ONLY_FAILED=1
      ;;
    --jobs)
      JOBS="${2:-}"
      shift
      ;;
    --machines)
      machines_arg="${2:-}"
      shift
      if [[ -z "$machines_arg" ]]; then
        echo "Missing value for --machines" >&2
        exit 1
      fi
      machines_arg=${machines_arg//,/ }
      read -r -a MACHINES <<<"$machines_arg"
      if ((${#MACHINES[@]} == 0)); then
        echo "No machines provided to --machines" >&2
        exit 1
      fi
      ;;
    --)
      shift
      ROLE_ARGS+=("$@")
      break
      ;;
    *)
      ROLE_ARGS+=("$1")
      ;;
  esac
  shift
done

if [[ -z "${JOBS:-}" ]]; then
  echo "Missing value for --jobs" >&2
  exit 1
fi

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

cleanup() {
  if [[ -n "${PARALLEL_PID:-}" ]]; then
    pkill -TERM -P "$PARALLEL_PID" 2>/dev/null || true
    kill "$PARALLEL_PID" 2>/dev/null || true
    wait "$PARALLEL_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

run_role() {
  local script=$1
  shift

  # Parallel appends the role name last; everything in between forwards to testrole.sh
  local args=("$@")
  local role="${args[-1]}"
  local machine="${args[-2]}"

  (
    "$script" --checkmode "${args[@]}" 2> >(colorize_stderr)
  ) &>"$OUT_DIR/$role.$machine.ansi" || {
    printf '\e[0;41m%s failed\e[0m\n' "$role.$machine" >&2
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

PARALLEL=(parallel --jobs "$JOBS" --joblog "$LOG_FILE" --eta run_role test/testrole.sh --machine {1} {2})
if (( ONLY_FAILED )); then
  mapfile -t roles < <(test/showfailed.sh)
  if ((${#roles[@]} == 0)); then
    echo "No failed roles recorded in $LOG_FILE" >&2
    exit 0
  fi
else
  mapfile -t roles < <(list_roles)
  if ((${#roles[@]} == 0)); then
    echo "No roles with tasks/main.yml found" >&2
    exit 1
  fi
fi

"${PARALLEL[@]}" "${ROLE_ARGS[@]}" ::: "${MACHINES[@]}" ::: "${roles[@]}" &
PARALLEL_PID=$!
wait "$PARALLEL_PID"
PARALLEL_PID=""

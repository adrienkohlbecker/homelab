#!/usr/bin/env bash
#MISE description="Run the full role-test matrix in parallel"
#USAGE flag "--retry-failed" help="Rerun only the cells that failed in test/out.tsv"
#USAGE flag "--jobs <jobs>" default="5" help="Number of cells to run at once"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

command -v parallel >/dev/null || {
  echo "test:all needs GNU parallel: brew install parallel, or apt install parallel" >&2
  exit 1
}

joblog=test/out.tsv
# Each testrole.py prints tagged, role-coloured status lines, so whole lines
# interleave readably. On Ctrl-C, SIGINT gives each cell 30s to stop its VM.
parallel_args=(
  --jobs "${usage_jobs}"
  --line-buffer
  --joblog "${joblog}"
  --termseq 'INT,30000,TERM,5000,KILL,25'
)

status=0
if [ "${usage_retry_failed:-false}" = true ]; then
  parallel "${parallel_args[@]}" --retry-failed || status=$?
else
  test/matrix.py | parallel "${parallel_args[@]}" --colsep '\t' \
    test/testrole.py --machine '{1}' --ubuntu '{2}' '{3}' || status=$?
fi

# The joblog's Exitval is column 7 and the command column 9.
if [ "${status}" -ne 0 ]; then
  echo
  echo "Failed cells (rerun with: mise run test:all --retry-failed):"
  awk -F '\t' 'NR > 1 && $7 != 0 { print "  " $9 "  (exit " $7 ")" }' "${joblog}"
fi
exit "${status}"

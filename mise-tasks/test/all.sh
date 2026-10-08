#!/usr/bin/env bash
#MISE description="Run the full role-test matrix in parallel"
#USAGE flag "--retry-failed" help="Rerun the cells that failed or never started in the last run (test/out.tsv)"
#USAGE flag "--jobs <jobs>" default="5" help="Number of cells to run at once (pass after --: mise run consumes its own --jobs)"
#USAGE arg "[testrole_args]..." var=#true help="Extra test/testrole.py arguments for every cell, e.g. --upstream-mirrors or --verbose"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

command -v parallel >/dev/null || {
  echo "test:all needs GNU parallel: brew install parallel, or apt install parallel" >&2
  exit 1
}
if ! [[ "${usage_jobs}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--jobs must be a positive number of cells, got '${usage_jobs}'" >&2
  exit 1
fi

joblog=test/out.tsv
# Each testrole.py prints tagged, role-coloured status lines, so whole lines
# interleave readably. On Ctrl-C, SIGINT gives each cell 30s to stop its VM.
parallel_args=(
  --jobs "${usage_jobs}"
  --line-buffer
  --joblog "${joblog}"
  --termseq 'INT,30000,TERM,5000,KILL,25'
)
if [ "${usage_retry_failed:-false}" = true ]; then
  # --resume-failed replays the same matrix against the joblog by sequence
  # number: it reruns failed cells and starts the ones an interrupt kept from
  # starting. A joblog from before GNU parallel ran the matrix has its own
  # columns; set it aside and run every cell.
  if [ -f "${joblog}" ] && [ "$(head -c 4 "${joblog}")" != Seq$'\t' ]; then
    mv "${joblog}" "${joblog}.legacy"
    echo "Moved an old-format ${joblog} to ${joblog}.legacy; running every cell" >&2
  fi
  parallel_args+=(--resume-failed)
fi

# mise passes every task argument in "$@", this task's own flags included;
# usage_testrole_args holds just the forwarded ones, shell-quoted. GNU parallel
# hands the command line to a shell, so quote them once more for it.
eval "testrole_args=(${usage_testrole_args:-})"
forwarded=""
if [ "${#testrole_args[@]}" -gt 0 ]; then
  forwarded=$(printf ' %q' "${testrole_args[@]}")
fi

status=0
test/matrix.py | parallel "${parallel_args[@]}" --colsep '\t' \
  "test/testrole.py --machine {1} --ubuntu {2} {3}${forwarded}" || status=$?

# A retry appends to the joblog, so judge each cell (Seq, column 1) by its
# latest row: Exitval is column 7 and the command column 9.
if [ "${status}" -ne 0 ]; then
  echo
  echo "Failed cells (rerun with: mise run test:all --retry-failed; it also starts cells an interrupt skipped):"
  awk -F '\t' '
    NR > 1 { exitval[$1] = $7; command[$1] = $9; if ($1 > last) last = $1 }
    END { for (seq = 1; seq <= last; seq++) if (seq in exitval && exitval[seq] != 0) print "  " command[seq] "  (exit " exitval[seq] ")" }
  ' "${joblog}"
fi
exit "${status}"

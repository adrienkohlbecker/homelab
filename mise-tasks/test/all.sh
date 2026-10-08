#!/usr/bin/env bash
#MISE description="Run the full role-test matrix in parallel"
#USAGE flag "--retry-failed" help="Rerun the cells that failed or never started in the last run (test/out.tsv)"
#USAGE flag "--jobs <jobs>" default="5" help="Number of cells to run at once (pass after --: mise run consumes its own --jobs)"
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
# The cells of the last full run. --resume-failed matches the joblog to its
# input by sequence number, so a retry must replay exactly this list: a
# regenerated matrix with a role added or removed would shift every number.
cells=test/out.cells.tsv
# Each testrole.py prints tagged, role-coloured status lines, so whole lines
# interleave readably. On Ctrl-C, SIGINT gives each cell 30s to stop its VM.
parallel_args=(
  --jobs "${usage_jobs}"
  --line-buffer
  --joblog "${joblog}"
  --termseq 'INT,30000,TERM,5000,KILL,25'
)
full_run=true
if [ "${usage_retry_failed:-false}" = true ]; then
  # --resume-failed reruns failed cells and starts the ones an interrupt kept
  # from starting. The test:all that predates GNU parallel wrote its own
  # columns to the joblog; archive that, and refuse anything else unknown.
  header=""
  if [ -s "${joblog}" ]; then
    header=$(head -n 1 "${joblog}")
  fi
  if [ "${header}" = $'Role\tUbuntu\tMachine\tRuntime\tExitval\tStarted' ]; then
    archive="${joblog}.legacy.$(date +%Y%m%d%H%M%S).$$"
    mv "${joblog}" "${archive}"
    echo "Archived an old-format ${joblog} as ${archive}; running every cell" >&2
  elif [ -n "${header}" ] && [[ "${header}" != Seq$'\t'* ]]; then
    echo "${joblog} is not a GNU parallel joblog; delete it to run every cell" >&2
    exit 1
  elif [ -s "${joblog}" ] && [ -f "${cells}" ]; then
    full_run=false
    parallel_args+=(--resume-failed)
  else
    echo "No previous run to retry; running every cell" >&2
  fi
fi
if [ "${full_run}" = true ]; then
  test/matrix.py >"${cells}.tmp"
  mv "${cells}.tmp" "${cells}"
fi

status=0
parallel "${parallel_args[@]}" --colsep '\t' --arg-file "${cells}" \
  test/testrole.py --machine '{1}' --ubuntu '{2}' '{3}' || status=$?

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

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
# Each testrole.py prints tagged, role-coloured status lines. On a terminal,
# --latest-line gives each running cell one line showing its newest status
# and leaves a finished cell's verdict behind; elsewhere whole lines
# interleave readably. On Ctrl-C, SIGINT gives each cell 30s to stop its VM.
parallel_args=(
  --jobs "${usage_jobs}"
  --joblog "${joblog}"
  --termseq 'INT,30000,TERM,5000,KILL,25'
)
if [ -t 1 ]; then
  parallel_args+=(--latest-line)
else
  parallel_args+=(--line-buffer)
fi
full_run=true
if [ "${usage_retry_failed:-false}" = true ]; then
  # --resume-failed reruns failed cells and starts the ones an interrupt kept
  # from starting. Refuse a joblog GNU parallel did not write.
  header=""
  if [ -s "${joblog}" ]; then
    header=$(head -n 1 "${joblog}")
  fi
  if [ -n "${header}" ] && [[ "${header}" != Seq$'\t'* ]]; then
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
# latest row: Exitval is column 7 and the command column 9. Seq is also the
# cell's line in the cells file, whose machine, ubuntu, and role columns name
# its test/out files.
if [ "${status}" -ne 0 ]; then
  failed=$(awk -F '\t' '
    FNR == NR { cell[FNR] = $1 "." $2 "." $3; next }
    FNR > 1 { exitval[$1] = $7; command[$1] = $9; if ($1 > last) last = $1 }
    END { for (seq = 1; seq <= last; seq++) if (seq in exitval && exitval[seq] != 0) print cell[seq] "\t" exitval[seq] "\t" command[seq] }
  ' "${cells}" "${joblog}")
  # Reprint what each failed cell showed when it failed, which --latest-line
  # has since reduced to its verdict; a cell killed before reporting has none.
  summary=""
  while IFS=$'\t' read -r cell exitval command; do
    [ -n "${cell}" ] || continue
    failure="test/out/${cell}.failure.ansi"
    if [ -f "${failure}" ]; then
      echo
      echo "${cell} (log: test/out/${cell}.output.ansi)"
      sed 's/^/│ /' "${failure}"
    fi
    summary+="  ${command}  (exit ${exitval})"$'\n'
  done <<<"${failed}"
  echo
  echo "Failed cells (rerun with: mise run test:all --retry-failed; it also starts cells an interrupt skipped):"
  printf '%s' "${summary}"
fi
exit "${status}"

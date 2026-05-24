# shellcheck shell=bash
# Sourced preamble for repo operator scripts.
#
# Side effects on caller (load-bearing; no consumer re-declares these):
#   - set -Eeuo pipefail + shopt -s inherit_errexit (strict mode that
#     also propagates errexit into $(...) and ERR traps into functions)
#   - PATH pinned to system dirs (callers run via roles/systemd_timer,
#     which inherits /etc/environment, not /etc/profile)
#   - ERR trap installed (f_err_trap): on any failure that trips errexit,
#     prints "ERR: <file>:<line> in <func>: <cmd> (exit N)" to stderr
#     before the shell terminates. Override with `trap - ERR` if needed.

set -Eeuo pipefail
shopt -s inherit_errexit

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Default ERR trap. -E (errtrace) above propagates this into functions
# and $(...) subshells; without errtrace the trap wouldn't fire for
# failures inside command substitution or in helpers. Commands in
# `if`/`while`/`until` conditions and `&&`/`||` chains (except the
# last) are exempt from ERR per bash semantics -- so f_rescue's
# `if "$@"; then ... else ...` won't trigger this trap.
f_err_trap() {
  local rc=$?
  local lineno=$1 cmd=$2
  # When the trap fires from top-level (no function frame above the
  # trap), BASH_SOURCE[1] is unset, which trips `set -u` before the
  # ##*/ strip can run. Fall back to the script that defined the trap
  # so the breadcrumb still names *something*; same shape as the
  # :-main default below for FUNCNAME[1].
  local src=${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}
  src=${src##*/}
  local func=${FUNCNAME[1]:-main}
  printf 'ERR: %s:%s in %s: %s (exit %s)\n' "$src" "$lineno" "$func" "$cmd" "$rc" >&2
}
trap 'f_err_trap "$LINENO" "$BASH_COMMAND"' ERR

# Run "$@" once; on non-zero exit, log to stderr and bump f_failed.
# Uses an `if`/`else` rather than `set +e`/`set -e` so the caller's
# errexit state is never mutated (the prior toggle pattern could
# silently re-enable -e in a caller that had deliberately disabled it).
#
# Caveats:
#   - f_rescue inside a subshell (parentheses or RHS of a pipeline)
#     updates a copy of f_failed; the parent count stays at zero.
#   - The callee must be an external command or subshell. Calling
#     f_rescue with a shell function suppresses `set -e` inside that
#     function (bash if-condition rule), so internal failures are
#     silently ignored -- wrap in `bash -c '...'` to force a subshell.
declare -gi f_failed=0
f_rescue() {
  if "$@"; then
    return 0
  else
    # `$?` inside `else` is the condition's exit code; after `fi` it
    # would be 0 (per `man bash`: "zero if no condition tested true").
    local retval=$?
    echo >&2 "Error:$(printf ' %q' "$@") failed with exit $retval"
    ((f_failed += 1))
  fi
}

# Assert effective uid is 0. (( )) form: EUID is a bash integer special.
f_require_root() {
  if (( EUID != 0 )); then
    echo >&2 "Error: I require root"
    exit 1
  fi
}

# Echo a shell-quoted trace banner, then exec the command. Banner goes
# to stdout, NOT stderr: every consumer runs under systemd_timer's
# stderr_priority wrapper (stderr -> journal priority err), so an
# informational banner on stderr mislabelled every traced command as an
# error and polluted `journalctl -p err`. stdout lands at info, leaving
# -p err for real failures. (No cron consumers remain; keeping the
# banner off the cron-mailed stdout stream was the old stderr rationale.)
f_trace() {
  printf '$%s\n' "$(printf ' %q' "$@")"
  "$@"
}

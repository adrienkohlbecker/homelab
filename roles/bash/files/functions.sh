# shellcheck shell=bash
# Sourced preamble for repo operator scripts.
#
# Side effects on caller (load-bearing; no consumer re-declares these):
#   - set -Eeuo pipefail + shopt -s inherit_errexit (strict mode that
#     also propagates errexit into $(...) and ERR traps into functions)
#   - PATH pinned to system dirs (callers run via roles/systemd_timer,
#     which inherits /etc/environment, not /etc/profile)

set -Eeuo pipefail
shopt -s inherit_errexit

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Print to stderr. printf, not echo, so a leading "-n"/"-e"/"-E" in $1
# is treated as data, not a flag.
f_error() {
  printf '%s\n' "$*" >&2
}

# f_error + exit 1. Constraint: when sourced from a shell function or
# `bash -c`, `exit 1` terminates the enclosing shell -- any callee that
# f_rescue is meant to recover from must therefore be an external
# command or a subshell, not a shell function in the same process.
f_fail() {
  f_error "$@"
  exit 1
}

# Run "$@" once; on non-zero exit, log to stderr and bump f_failed.
# Uses an `if`/`else` rather than `set +e`/`set -e` so the caller's
# errexit state is never mutated (the prior toggle pattern could
# silently re-enable -e in a caller that had deliberately disabled it).
#
# Caveats:
#   - f_rescue inside a subshell (parentheses or RHS of a pipeline)
#     updates a copy of f_failed; the parent count stays at zero.
#   - The callee must be an external command or subshell -- a shell
#     function that calls f_fail terminates the entire script.
declare -gi f_failed=0
f_rescue() {
  if "$@"; then
    return 0
  else
    # `$?` inside `else` is the condition's exit code; after `fi` it
    # would be 0 (per `man bash`: "zero if no condition tested true").
    local retval=$?
    f_error "Error:$(printf ' %q' "$@") failed with exit $retval"
    ((f_failed += 1))
  fi
}

# Assert effective uid is 0. (( )) form: EUID is a bash integer special.
f_require_root() {
  if (( EUID != 0 )); then
    f_fail "Error: I require root"
  fi
}

# Echo a shell-quoted form of the argv to stderr (so the trace banner
# doesn't pollute the cron-mailed stdout stream), then exec the command.
f_trace() {
  printf '$%s\n' "$(printf ' %q' "$@")" >&2
  "$@"
}

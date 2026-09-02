# shellcheck shell=bash
# Sourced preamble for repo operator scripts.
#
# Side effects on caller (load-bearing; no consumer re-declares these):
#   - set -euo pipefail + shopt -s inherit_errexit (strict mode that
#     also propagates errexit into $(...))
#   - PATH pinned to system dirs (callers run via roles/systemd_timer,
#     which inherits /etc/environment, not /etc/profile)

set -euo pipefail
shopt -s inherit_errexit

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

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
  if ((EUID != 0)); then
    echo >&2 "Error: I require root"
    exit 1
  fi
}

# Echo a shell-quoted trace banner, then exec the command. Banner goes
# to stdout, NOT stderr: every consumer runs under systemd_timer's
# systemd-cat transport, which assigns stderr journal priority err. An
# informational banner on stderr would mislabel every traced command and
# pollute `journalctl -p err`; stdout stays at info and leaves -p err for
# real failures.
f_trace() {
  printf '$%s\n' "$(printf ' %q' "$@")"
  "$@"
}

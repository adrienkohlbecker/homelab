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

# Run "$@" once; on failure, log it and increment f_failed without changing
# the caller's errexit state. Function callees must return the wrapped command's
# status; a subshell updates only its copy of f_failed.
declare -gi f_failed=0
f_rescue() {
  "$@" || {
    local retval=$?
    echo >&2 "Error:$(printf ' %q' "$@") failed with exit $retval"
    ((f_failed += 1))
  }
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

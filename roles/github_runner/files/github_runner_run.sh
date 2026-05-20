#!/bin/sh
# Thin launcher for actions/runner from a per-instance dir. Invoked
# from github_runner@<inst>.service as ExecStart=/usr/local/bin/
# github_runner_run %i. The wrapper exists because systemd-analyze
# verify (called by systemd_unit's install.yml validation) substitutes
# "i" as the placeholder instance name when verifying a template unit,
# so an ExecStart like /opt/actions-runner/%i/run.sh would fail
# validation with "Command /opt/actions-runner/i/run.sh is not
# executable: No such file or directory". A stable /usr/local/bin
# path satisfies the verify; the instance dir gets resolved at
# runtime.
#
# Also asserts the per-instance dir exists before exec'ing run.sh so a
# misconfigured unit (orphan dir removed but unit not stopped, etc.)
# surfaces a clear error in the journal instead of a cryptic "No such
# file or directory" from /bin/sh.
set -eu

inst="${1:?usage: $0 <instance>}"
dir="/opt/actions-runner/${inst}"

if [ ! -d "$dir" ]; then
  echo >&2 "github_runner_run: $dir missing -- has _register.yml run for this instance?"
  exit 1
fi

cd "$dir"
exec ./run.sh

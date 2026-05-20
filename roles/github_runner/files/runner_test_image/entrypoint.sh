#!/bin/sh
# Test-only stub. Validates the bind-mounts the real entrypoint relies
# on -- /run/jit (must be readable from the in-container effective uid;
# catches JIT-file-ownership regressions) and /opt/actions-runner (the
# unit binds the host-side per-instance /opt/actions-runner/<inst> at
# this in-container path; sync_root ExecStartPre populates it with
# hard links from the canonical) -- then daemonizes a process named
# "Runner.Listener" so the unit's `pgrep --exact Runner.Listener`
# healthcheck reaches healthy and the unit settles into active.
set -eu

test -r /run/jit || { echo >&2 "/run/jit not readable from in-container uid"; exit 1; }
# run.sh is hard-linked into the bound per-instance dir by sync_root;
# its absence means either the bind-mount target is wrong or the host-
# side sync_root didn't fire / didn't populate the dir.
test -x /opt/actions-runner/run.sh || { echo >&2 "/opt/actions-runner/run.sh missing; bind-mount or sync_root regression?"; exit 1; }

# /proc/<pid>/comm is set from the basename of the executed binary at
# execve. cp /bin/sleep (a standalone GNU coreutils binary in debian)
# to /tmp/Runner.Listener gives us that comm without baking a custom
# binary into the image. "Runner.Listener" (15 chars) fits in
# TASK_COMM_LEN (16).
cp /bin/sleep /tmp/Runner.Listener
exec /tmp/Runner.Listener infinity

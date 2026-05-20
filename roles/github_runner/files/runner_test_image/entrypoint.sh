#!/bin/sh
# Test-only stub. Validates the bind-mounts the real entrypoint relies
# on -- /run/jit (must be readable from the in-container effective uid;
# catches JIT-file-ownership regressions) and the per-instance
# /opt/actions-runner/<inst> dir (the unit's --workdir sets $PWD to it
# and github_runner_sync_root ExecStartPre populates it on the host) --
# then daemonizes a process named "Runner.Listener" so the unit's
# `pgrep --exact Runner.Listener` healthcheck reaches healthy and the
# unit settles into active.
set -eu

test -r /run/jit || { echo >&2 "/run/jit not readable from in-container uid"; exit 1; }
# run.sh is hard-linked into every per-instance dir by sync_root. Its
# absence in $PWD means either the bind-mount target is wrong or the
# host-side sync_root didn't fire / didn't populate the dir.
test -x ./run.sh || { echo >&2 "run.sh missing from $PWD; bind-mount or sync_root regression?"; exit 1; }

# /proc/<pid>/comm is set from the basename of the executed binary at
# execve. cp /bin/sleep (a standalone GNU coreutils binary in debian)
# to /tmp/Runner.Listener gives us that comm without baking a custom
# binary into the image. "Runner.Listener" (15 chars) fits in
# TASK_COMM_LEN (16).
cp /bin/sleep /tmp/Runner.Listener
exec /tmp/Runner.Listener infinity

#!/bin/sh
# Test-only stub. Validates the bind-mounts the real entrypoint relies
# on -- /run/jit (must be readable from the in-container effective uid;
# catches JIT-file-ownership regressions) and /opt/actions-runner
# (must be mounted; catches bind-mount failures including the :O
# overlay-vs-uidmap clash) -- then daemonizes a process named
# "Runner.Listener" so the unit's `pgrep --exact Runner.Listener`
# healthcheck reaches healthy and the unit settles into active.
set -eu

test -r /run/jit || { echo >&2 "/run/jit not readable from in-container uid"; exit 1; }
test -d /opt/actions-runner || { echo >&2 "/opt/actions-runner not mounted"; exit 1; }

# /proc/<pid>/comm is set from the basename of the executed binary at
# execve. cp /bin/sleep (a standalone GNU coreutils binary in debian)
# to /tmp/Runner.Listener gives us that comm without baking a custom
# binary into the image. "Runner.Listener" (15 chars) fits in
# TASK_COMM_LEN (16).
cp /bin/sleep /tmp/Runner.Listener
exec /tmp/Runner.Listener infinity

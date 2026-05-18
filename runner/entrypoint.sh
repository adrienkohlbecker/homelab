#!/bin/bash
# Container entrypoint for the lab's GitHub Actions runners.
#
# Ephemeral + JIT model: each container start consumes a fresh, single-
# use JIT config that was already minted by the host-side ExecStartPre
# (/usr/local/bin/github_runner_jit_mint) and bind-mounted in at
# /run/jit:ro. The runner registers with that blob, grabs one job, runs
# it, deregisters itself, and exits cleanly. systemd's Restart=always
# on github_runner@.service brings up the next container, whose
# ExecStartPre mints a fresh blob first.
#
# Path-mirror story: /opt/actions-runner is bind-mounted from the host
# at the same path so DooD-spawned workflow containers can resolve
# their bind-mount sources. /<RUNNER_WORK_FOLDER> is similarly path-
# mirrored.
#
# Security shape: the host's long-lived PAT NEVER enters this
# container. Only the single-use JIT blob does (which GitHub auto-
# invalidates after first use). Workflow code running here can read
# /run/jit, but that buys nothing -- the blob is bound to one
# registration and is already consumed by run.sh. The PAT itself lives
# only in root podman's secret store on the host and is read solely
# by the mint script running as the unit's user.
set -euo pipefail

cd /opt/actions-runner

jit=$(cat /run/jit)
if [ -z "$jit" ]; then
  echo >&2 "/run/jit is empty; ExecStartPre mint failed silently?"
  exit 1
fi

# run-helper.sh.template (invoked by run.sh in a loop) refuses to
# proceed when `id -u` returns 0 unless RUNNER_ALLOW_RUNASROOT is set
# ("Must not run interactively with sudo"). The runner image's
# entrypoint deliberately runs as in-container root -- the uidmap on
# the systemd unit maps that to host github_runner, so the "root"
# inside the namespace is functionally a low-privilege user. Opt out
# of the runtime check explicitly.
export RUNNER_ALLOW_RUNASROOT=1

# The JIT blob carries `ephemeral: true` server-side, so the runner
# self-deregisters on clean exit; no explicit --ephemeral flag needed.
# --disableupdate intentionally omitted: ephemeral runners exit after
# one job, so a mid-run self-update never finishes; passing the flag in
# JIT mode would also conflict with run.sh's argument parser.
exec ./run.sh --jitconfig "$jit"

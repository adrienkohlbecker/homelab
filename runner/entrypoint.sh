#!/bin/bash
# Container entrypoint for the lab's GitHub Actions runners.
#
# Ephemeral + JIT model: every container start mints a fresh, single-
# use JIT config from GitHub's /actions/runners/generate-jitconfig
# endpoint, then execs actions/runner's run.sh --jitconfig <blob>.
# The runner registers, grabs one job, runs it, deregisters itself,
# and exits cleanly. systemd's Restart=always on github_runner@.service
# brings up the next container, which mints its own JIT blob.
#
# No persistent runner state -- /actions-runner is image-baked and not
# bind-mounted, and the runner doesn't survive past one job. /work is
# the only volume (job checkout root, per-instance, kept across jobs
# for cache locality but reaped by the host's tmpfiles.d rule).
#
# Required env (injected via the systemd unit's EnvironmentFile and
# --secret mount):
#   GITHUB_PAT_FILE   path to a podman-secret-mount file containing
#                     the host's fine-grained PAT (scope: repo +
#                     actions:write on REPO_OWNER/REPO_NAME). Mount-
#                     type, not env, so the value never lands in
#                     /proc/<pid>/environ visible via DooD `docker exec`.
#   REPO_OWNER        GitHub owner (e.g. adrienkohlbecker).
#   REPO_NAME         GitHub repo (e.g. homelab).
#   RUNNER_NAME       single-instance name as it appears in the GitHub
#                     UI; deterministic, derived from <repo>_<suffix>.
#   RUNNER_LABELS     comma-separated label set (e.g.
#                     "self-hosted,lab-vm").
set -euo pipefail

: "${GITHUB_PAT_FILE:?required (podman-secret-mount path)}"
: "${REPO_OWNER:?required}"
: "${REPO_NAME:?required}"
: "${RUNNER_NAME:?required}"
: "${RUNNER_LABELS:?required}"

cd /actions-runner

# Append a per-start random suffix to the runner name so a crash
# AFTER the `exec ./run.sh` below (i.e. past where the trap fires
# but before run.sh self-deregisters) doesn't block subsequent
# starts with HTTP 409 ("runner with this name already exists").
# Each container start gets a fresh unique registration; the trap
# still catches the exit-before-exec case for cleanly-deregistered
# names. Old offline records from genuine crashes accumulate on
# GitHub's runner list but don't gate future starts -- periodic
# manual cleanup via `gh api -X DELETE` if the list grows
# inconveniently.
random_suffix=$(tr -d '-' < /proc/sys/kernel/random/uuid | head -c 8)
runner_name="${RUNNER_NAME}_${random_suffix}"

# Build the request body. jq -Rcn 'input | split(",")' converts the
# comma-separated label env into a JSON array; --arg / --argjson on
# the outer jq stitch it into the final object without manual quoting.
labels_json=$(echo "$RUNNER_LABELS" | jq -Rcn 'input | split(",")')
body=$(jq -cn \
  --arg name "$runner_name" \
  --argjson labels "$labels_json" \
  '{name:$name, runner_group_id:1, labels:$labels, work_folder:"/work"}')

# Mint the single-use JIT config. runner_group_id 1 is the implicit
# default group present on every personal-account repo; org/enterprise
# setups can override but homelab is personal-account-only. The PAT is
# the long-lived secret operators manage out-of-band (1Password →
# vaulted host_vars → podman secret), shared across every runner
# instance on the host (the API call is per-repo, not per-runner).
#
# Capture the full response (not just .encoded_jit_config) so we can
# extract .runner.id for the cleanup trap below. generate-jitconfig
# registers the runner the moment it returns 201 -- if we exit
# between here and a successful exec of run.sh, the registration
# leaks and the next start hits HTTP 409 ("runner with this name
# already exists"). run.sh + ephemeral handles deregistration once
# it's running; we need to handle the gap.
jit_response=$(curl --fail --silent --show-error \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $(cat "$GITHUB_PAT_FILE")" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -X POST "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runners/generate-jitconfig" \
  -d "$body")
jit=$(echo "$jit_response" | jq -r .encoded_jit_config)
runner_id=$(echo "$jit_response" | jq -r .runner.id)

# Cleanup trap: if the shell exits before `exec` replaces it (e.g.
# run.sh missing from a broken image, a bug in the exec path, any
# `set -e` trigger after jit_response is set), DELETE the just-
# registered runner so the next start doesn't 409 forever. After
# exec succeeds the shell process is gone and this trap is gone with
# it -- run.sh's ephemeral self-deregistration handles the
# clean-exit case. Crash-mid-job (run.sh dies between exec and
# completion) still leaves a stale registration; that's a much
# rarer mode and outside this trap's scope (would need ExecStopPost
# or an external reconciler to handle generally).
cleanup_runner() {
  rc=$?
  if [ -n "${runner_id:-}" ] && [ "$runner_id" != "null" ]; then
    echo >&2 "entrypoint exiting rc=${rc} with active runner registration ${runner_id}; deleting via API"
    curl --silent --show-error --fail-with-body \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer $(cat "$GITHUB_PAT_FILE")" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -X DELETE \
      "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runners/${runner_id}" \
      || echo >&2 "WARN: failed to delete runner ${runner_id}; manual cleanup may be needed"
  fi
}
trap cleanup_runner EXIT

# run-helper.sh.template (invoked by run.sh in a loop) refuses to
# proceed when `id -u` returns 0 unless RUNNER_ALLOW_RUNASROOT is
# set ("Must not run interactively with sudo"). The runner image's
# entrypoint deliberately runs as in-container root -- the uidmap on
# the systemd unit maps that to host github_runner, so the "root"
# inside the namespace is functionally a low-privilege user. Opt out
# of the runtime check explicitly.
export RUNNER_ALLOW_RUNASROOT=1

# The JIT blob carries `ephemeral: true` server-side, so the runner
# self-deregisters on clean exit; no explicit --ephemeral flag needed.
# --disableupdate is intentionally omitted: ephemeral runners exit
# after one job, so a mid-run self-update never finishes; passing the
# flag in JIT mode would also conflict with run.sh's argument parser.
exec ./run.sh --jitconfig "$jit"

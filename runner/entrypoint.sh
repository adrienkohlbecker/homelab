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

# Build the request body. jq -Rcn 'input | split(",")' converts the
# comma-separated label env into a JSON array; --arg / --argjson on
# the outer jq stitch it into the final object without manual quoting.
labels_json=$(echo "$RUNNER_LABELS" | jq -Rcn 'input | split(",")')
body=$(jq -cn \
  --arg name "$RUNNER_NAME" \
  --argjson labels "$labels_json" \
  '{name:$name, runner_group_id:1, labels:$labels, work_folder:"/work"}')

# Mint the single-use JIT config. runner_group_id 1 is the implicit
# default group present on every personal-account repo; org/enterprise
# setups can override but homelab is personal-account-only. The PAT is
# the long-lived secret operators manage out-of-band (1Password →
# vaulted host_vars → podman secret), shared across every runner
# instance on the host (the API call is per-repo, not per-runner).
jit=$(curl --fail --silent --show-error \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $(cat "$GITHUB_PAT_FILE")" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -X POST "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runners/generate-jitconfig" \
  -d "$body" \
  | jq -r .encoded_jit_config)

# The JIT blob carries `ephemeral: true` server-side, so the runner
# self-deregisters on clean exit; no explicit --ephemeral flag needed.
# --disableupdate is intentionally omitted: ephemeral runners exit
# after one job, so a mid-run self-update never finishes; passing the
# flag in JIT mode would also conflict with run.sh's argument parser.
exec ./run.sh --jitconfig "$jit"

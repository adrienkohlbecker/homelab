#!/bin/bash
# Mint a single-use JIT registration blob for a github_runner@%i
# instance and write it to /run/<unit>/jit (RuntimeDirectory=%N on the
# unit creates that dir at start). The runner container bind-mounts
# this file at /run/jit:ro and consumes it instead of doing the mint
# itself -- so the host's long-lived PAT never enters the container,
# and workflow code running inside the runner can read /run/jit (a
# single-use, bounded-time, runner-scoped credential) but not the PAT
# it was minted from.
#
# Random 8-char suffix appended to RUNNER_NAME so a runner that exits
# uncleanly (entrypoint crash, run.sh segfault, SIGKILL mid-job) doesn't
# block subsequent starts with HTTP 409 "runner with this name already
# exists". Orphan registrations linger as offline on GitHub's runner
# list; periodic `gh api -X DELETE` cleanup if it gets noisy.
set -euo pipefail

inst="${1:?usage: $0 <instance>}"

envfile="/etc/default/github_runner@${inst}"
[ -r "$envfile" ] || { echo >&2 "$0: $envfile not readable"; exit 1; }
# shellcheck disable=SC1090
. "$envfile"
: "${REPO_OWNER:?required in $envfile}"
: "${REPO_NAME:?required in $envfile}"
: "${RUNNER_NAME:?required in $envfile}"
: "${RUNNER_LABELS:?required in $envfile}"
: "${RUNNER_WORK_FOLDER:?required in $envfile}"
: "${RUNNER_GROUP_ID:?required in $envfile}"

# Host-only access path. The runner container has no --secret mount
# for the PAT anymore; only this script (running as the unit's user,
# root) can reach it via root podman's secret store.
pat=$(podman secret inspect --showsecret github_runner_pat -f '{{.SecretData}}')
[ -n "$pat" ] || { echo >&2 "$0: podman secret github_runner_pat empty"; exit 1; }

random_suffix=$(tr -d '-' < /proc/sys/kernel/random/uuid | head -c 8)
runner_name="${RUNNER_NAME}_${random_suffix}"

# Build the JIT-config request body. jq -Rcn 'input | split(",")' lifts
# the comma-separated label string into a JSON array; --arg / --argjson
# stitch it into the final object without manual quoting.
labels_json=$(echo "$RUNNER_LABELS" | jq -Rcn 'input | split(",")')
# runner_group_id flows from the per-instance envfile (default 1 for
# personal-account repos, override-able for organization runner groups).
# The PAT scope (Administration: R/W per CLAUDE.md) is what grants the
# generate-jitconfig call.
body=$(jq -cn \
  --arg name "$runner_name" \
  --argjson labels "$labels_json" \
  --arg work "$RUNNER_WORK_FOLDER" \
  --argjson group_id "$RUNNER_GROUP_ID" \
  '{name:$name, runner_group_id:$group_id, labels:$labels, work_folder:$work}')
jit=$(curl --fail --silent --show-error \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $pat" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -X POST "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runners/generate-jitconfig" \
  -d "$body" \
  | jq -r .encoded_jit_config)

if [ -z "$jit" ] || [ "$jit" = "null" ]; then
  echo >&2 "$0: generate-jitconfig returned empty"
  exit 1
fi

# RuntimeDirectory=%N on the unit creates /run/<unit>/ at start. Mode
# 0700 on the dir + 0600 on the file so only root reads it; the
# container's --volume mount maps in-container root (= host
# github_runner via the +0 uidmap override) to host root for the read,
# which works because the uidmap collapses both to the same in-userns
# uid.
out_dir="/run/github_runner@${inst}.service"
install -d -m 0700 "$out_dir"
umask 0077
printf '%s' "$jit" > "${out_dir}/jit"

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

# openssl avoids a tr|head SIGPIPE race that exits 141 under pipefail
# (tr writes the full 32-char uuid in one syscall today so head usually
# doesn't close the pipe in time, but the race is real). openssl is in
# the host's base install on ubuntu.
random_suffix=$(openssl rand -hex 4)
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
# --fail-with-body (not --fail): surface GitHub's error body on 4xx/5xx
# so a scope-revoked PAT / deleted repo / 422 prints actionable detail
# in the journal instead of curl's silent exit 22.
# --retry / --retry-all-errors / --retry-connrefused / --max-time: cover
# the boot-time race against nexus/DNS bringup without letting a stuck
# TCP socket sit in TimeoutStartSec for the full 300s. --retry-all-errors
# upgrades from curl's default transient set (408/429/5xx) to "retry every
# failure"; generate-jitconfig is safe to repeat (GitHub treats each call
# as a fresh mint) so the broader retry just hardens against TLS handshake
# resets and mid-response disconnects that would otherwise eat one of the
# unit's StartLimitBurst=3 on a transient blip.
#
# Authorization header is piped via `curl -K -` on stdin, not passed as
# `-H "Authorization: Bearer $pat"`. The shell expands $pat before
# exec'ing curl, so the bearer token would otherwise land in argv and be
# world-readable via /proc/<curl-pid>/cmdline for the lifetime of the
# call (~ms but observable). The unprivileged github_runner user owns
# the rootless DooD socket so a malicious workflow with `pid: host` on a
# sibling container could read it. printf is a bash builtin so no extra
# process inherits the secret.
jit=$(printf 'header = "Authorization: Bearer %s"\n' "$pat" \
  | curl --fail-with-body --silent --show-error \
    --retry 3 --retry-all-errors --retry-connrefused --max-time 20 \
    -K - \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -X POST "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runners/generate-jitconfig" \
    -d "$body" \
  | jq -r .encoded_jit_config)

if [ -z "$jit" ] || [ "$jit" = "null" ]; then
  echo >&2 "$0: generate-jitconfig returned empty"
  exit 1
fi

# RuntimeDirectory=%N on the unit creates /run/<%N>/ at start, where
# systemd's %N specifier strips the .service suffix -- so the
# bind-mount source on the unit (%t/%N/jit) and this script must agree
# on the no-suffix path. install -d is idempotent against the
# RuntimeDirectory-created dir and re-asserts 0700; the file lands at
# 0600 root:root via umask 0077. The container reads /run/jit through
# the bind-mount, not by re-resolving the file in its userns: podman
# opens the source path as the unit's process (real root) at container
# start, and the container reads through that already-opened fd. So
# in-container uid 0 (= host github_runner via the +0 uidmap) never
# has to satisfy a mode-0600-root-owned read check -- the host-side
# open already did. Don't relax the 0600/0700 modes; they're what keep
# the blob unreadable to anything other than this script + podman's
# open fd.
out_dir="/run/github_runner@${inst}"
install -d -m 0700 "$out_dir"
umask 0077
printf '%s' "$jit" > "${out_dir}/jit"

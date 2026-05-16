#!/bin/bash
# Container entrypoint for the lab's GitHub Actions runners.
#
# Two responsibilities, in order:
#  1. Sync actions/runner binaries from /runner-base into the /runner
#     volume on first start, or whenever the image's bundled version
#     doesn't match the version recorded in /runner. State files
#     (.runner, .credentials, _diag, _work) are preserved so version
#     bumps don't deregister the runner.
#  2. If /runner/.runner is absent (first start ever for this volume),
#     register against GitHub using the short-lived RUNNER_TOKEN that
#     roles/github_runner minted via `gh api`. Subsequent restarts
#     reuse the persisted .credentials and skip registration.
#
# After both run, exec actions/runner's run.sh as PID 1 so SIGTERM
# reaches the listener directly (run.sh forwards to Runner.Listener,
# which finishes the in-flight job before exiting).
set -euo pipefail

mkdir -p /runner

sentinel=/runner/.actions-runner-version
if [ ! -f "$sentinel" ] || [ "$(cat "$sentinel")" != "$ACTIONS_RUNNER_VERSION" ]; then
  # Excludes preserve everything the listener writes back to its install
  # dir at runtime. --delete-after removes binaries that disappeared
  # between versions (e.g. retired self-update helpers) without
  # touching the protected state files.
  rsync -a --delete-after \
    --exclude /.runner \
    --exclude /.credentials \
    --exclude /.credentials_rsaparams \
    --exclude /.path \
    --exclude /.env \
    --exclude /_diag \
    --exclude /_work \
    /runner-base/ /runner/
  echo "$ACTIONS_RUNNER_VERSION" > "$sentinel"
fi

cd /runner

if [ ! -f .runner ]; then
  : "${RUNNER_TOKEN:?required for first start (no /runner/.runner found)}"
  : "${REPO_URL:?required for first start}"
  : "${RUNNER_NAME:?required for first start}"
  : "${RUNNER_LABELS:?required for first start}"
  ./config.sh \
    --url "$REPO_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work /work \
    --unattended \
    --replace
fi

exec ./run.sh

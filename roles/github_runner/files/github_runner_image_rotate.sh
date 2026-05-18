#!/bin/bash
# Restart idle github_runner@*.service units whose container is running an
# image older than the latest in nexus. Skips units mid-job (Runner.Worker
# present in the container's process tree) -- those naturally pick up the
# new image when systemd respawns the container after the job completes
# via Restart=always + the default --pull=missing letting podman re-fetch
# the cached image (which the watcher has already refreshed in the local
# store via `podman pull` below).
#
# Closes the gap that an in-unit pull policy alone can't cover: an
# ephemeral runner only restarts after running a job, so a runner that
# registered hours ago and has been listening for jobs ever since keeps
# running the stale image until either a job lands on it (and it dies +
# respawns) or this timer rotates it.
set -euo pipefail

image="nexus.lab.fahm.fr/homelab/runner:latest"

# Sync the local store against the registry. Manifest HEAD is the
# expensive bit; layer pulls only happen when the digest moved. Soft-
# fail when nexus is unreachable -- if a cached image is in the local
# store from a prior successful pull, treat that as "latest" and
# proceed (a stale runner running an older digest still gets rotated
# to the cached image); otherwise there's nothing to compare against
# and we exit cleanly. stderr is wired to the journal so a chronic
# outage is visible in `journalctl -u github_runner_image_rotate`.
if ! podman pull --quiet "$image" >/dev/null 2>&1; then
  echo >&2 "podman pull of $image failed; checking local store for a cached image"
  if ! podman image exists "$image"; then
    echo >&2 "no cached $image either; skipping rotation this tick"
    exit 0
  fi
  echo >&2 "proceeding with cached $image (registry refresh deferred)"
fi

latest=$(podman image inspect "$image" --format '{{.Digest}}')

# Enumerate running runner containers directly from podman -- one query
# replaces the previous systemctl list-units + per-unit cidfile read
# chain. The container name encodes the systemd instance suffix
# (--name github_runner_%i in the unit template) so name -> unit is
# deterministic.
mapfile -t names < <(
  podman ps --filter "ancestor=$image" --format '{{.Names}}'
)

for name in "${names[@]}"; do
  [[ -z "$name" ]] && continue

  live=$(podman inspect "$name" --format '{{.ImageDigest}}' 2>/dev/null || true)
  [[ -z "$live" ]] && continue

  inst=${name#github_runner_}
  unit="github_runner@${inst}.service"

  if [[ "$live" == "$latest" ]]; then
    continue
  fi

  # Stale. Skip if a job is in flight: actions/runner only spawns
  # Runner.Worker for the duration of an active job, so its absence
  # in the container's process tree is the natural busy/idle signal.
  # A podman top failure (container in a transient state, racing
  # restart, etc.) is indistinguishable from "no Runner.Worker" via
  # grep alone -- treat any failure as busy and let the next tick
  # retry. Losing one rotation is cheap; killing a mid-job runner
  # isn't.
  if ! topout=$(podman top "$name" args 2>/dev/null); then
    echo "$inst: stale (live=${live:7:12} latest=${latest:7:12}) but podman top failed; treating as busy"
    continue
  fi
  if echo "$topout" | grep -q '[R]unner\.Worker'; then
    echo "$inst: stale (live=${live:7:12} latest=${latest:7:12}) but mid-job; skipping"
    continue
  fi

  echo "$inst: rotating (live=${live:7:12} -> latest=${latest:7:12})"
  systemctl restart "$unit"
done

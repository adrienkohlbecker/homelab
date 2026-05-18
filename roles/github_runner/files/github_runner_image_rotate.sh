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
# fail when nexus is unreachable -- the only consequence is delayed
# rotation; the next tick retries. set -e would otherwise turn a
# transient registry blip into a noisy unit failure.
if ! podman pull --quiet "$image" >/dev/null 2>&1; then
  echo >&2 "podman pull of $image failed; skipping rotation this tick"
  exit 0
fi

latest=$(podman image inspect "$image" --format '{{.Digest}}')

mapfile -t units < <(
  systemctl list-units --type=service --no-legend --plain --state=active \
    'github_runner@*.service' | awk '{print $1}'
)

rotated=0
busy=0
fresh=0

for unit in "${units[@]}"; do
  inst=${unit#github_runner@}
  inst=${inst%.service}

  cidfile="/run/github_runner@${inst}/${unit}.ctr-id"
  [[ -r "$cidfile" ]] || continue
  cid=$(<"$cidfile")
  [[ -z "$cid" ]] && continue

  live=$(podman inspect "$cid" --format '{{.ImageDigest}}' 2>/dev/null || true)
  [[ -z "$live" ]] && continue

  if [[ "$live" == "$latest" ]]; then
    fresh=$((fresh + 1))
    continue
  fi

  # Stale. Skip if a job is in flight: actions/runner only spawns
  # Runner.Worker for the duration of an active job, so its absence
  # in the container's process tree is the natural busy/idle signal.
  if podman top "$cid" args 2>/dev/null | grep -q '[R]unner\.Worker'; then
    echo "$inst: stale (live=${live:7:12} latest=${latest:7:12}) but mid-job; skipping"
    busy=$((busy + 1))
    continue
  fi

  echo "$inst: rotating (live=${live:7:12} -> latest=${latest:7:12})"
  systemctl restart "$unit"
  rotated=$((rotated + 1))
done

echo "rotated=$rotated busy=$busy fresh=$fresh"

#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

# Scrub every imported pool, one at a time, waiting for each scrub to finish.
# Scheduled monthly by the zfs_scrub timer (second Sunday), replacing the
# distro's /etc/cron.d/zfsutils-linux, which the zfs role deletes. Iterating
# `zpool list` rather than a hand-maintained pool list means a newly-added pool
# is scrubbed automatically.
#
# The exit status is the scrub outcome, so the unit's failed state is the
# alarm: it fails when a scrub cannot start, does not complete (canceled or
# paused), or leaves its pool unhealthy per `zpool status -x` (device errors,
# checksum errors the scrub found). `zpool scrub -w` itself exits 0 once the
# scrub stops running, however it stopped, so the scan line is checked
# afterwards. A multi-TB pool scrubs for hours (pug apoc ~17h), which the
# unit's TimeoutStartSec has to cover.
#
# Serial scrubs also keep the pools from entering their metadata-read bursts at
# the same instant: lab hard-locked ~12s into a parallel kick-off on 2026-06-14.
#
# zpool owns the per-pool scan state: it resumes a paused scrub and refuses to
# start one while a scrub or resilver is running, so a long scan is never
# restarted from zero. A refusal counts as a failure here; it takes a resilver
# or a scrub outlasting a month, both worth a look.
export LC_ALL=C

failed=0
fail() {
  echo >&2 "Error: $1"
  ((failed += 1))
}

pools=$(timeout -k 10 60 zpool list -H -o name)
for pool in $pools; do
  echo "Scrubbing $pool"
  if ! zpool scrub -w "$pool"; then
    fail "zpool scrub -w $pool failed"
    continue
  fi
  status=$(zpool status "$pool")
  if [[ "$status" != *"scan: scrub repaired"* ]]; then
    fail "scrub on $pool did not complete: $(awk '/scan:/' <<<"$status")"
    continue
  fi
  health=$(zpool status -x "$pool")
  if [ "$health" != "pool '$pool' is healthy" ]; then
    fail "$pool is not healthy after its scrub:"$'\n'"$health"
  fi
done

if ((failed != 0)); then
  echo >&2 "Error: $failed pool(s) failed their scrub"
  exit 1
fi
echo "Done"

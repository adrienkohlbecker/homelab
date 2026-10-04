#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

# Start a scrub on every imported pool. Scheduled monthly by the zfs_scrub timer
# (second Sunday), replacing the distro's /etc/cron.d/zfsutils-linux, which the
# zfs role deletes. Iterating `zpool list` rather than a hand-maintained pool
# list means a newly-added pool is scrubbed automatically and no per-host config
# can drift.
#
# Deliberately no -w: a multi-TB pool scrubs for hours (lab tank ~8h, pug rpool
# ~18h), so a blocking oneshot, or one serializing the pools, would fight
# TimeoutStartSec. The outcome is watched out-of-band: zfs_health alarms on
# canceled or stale scrubs (zfs_status.py scrub_issue); ZED's scrub_finish
# zedlet mails on scrubs with errors.
#
# Stagger the per-pool kick-offs instead. lab hard-locked ~12s into this run on
# 2026-06-14, during scrub initiation rather than steady state, so spacing the
# starts keeps every pool from entering its metadata-read burst in the same
# instant. Skipped (already-scrubbing) pools don't consume a slot, so the first
# pool actually kicked off waits for nothing. The stagger is overridable
# (ZFS_SCRUB_STAGGER_SEC) so the role's _verify can zero it -- the CI fixture's
# img-backed pools can't thundering-herd a hard lock.
stagger_sec="${ZFS_SCRUB_STAGGER_SEC:-120}"
scrub_started=0
failed=0
pools=$(timeout -k 10 60 zpool list -H -o name)
for pool in $pools; do
  # Skip a pool already scrubbing or resilvering: a fresh `zpool scrub` would
  # error out, and a long scrub spanning two monthly fires must not be
  # restarted from zero.
  active=$(/opt/zfs/zfs_status.py scan "$pool") || {
    echo >&2 "Error: cannot read scan state on $pool, skipping."
    ((failed += 1))
    continue
  }
  if [[ "$active" == 1 ]]; then
    echo "Scrub/resilver already running on $pool, skipping."
    continue
  fi
  if [ "$scrub_started" -ne 0 ]; then
    sleep "$stagger_sec"
  fi
  scrub_started=1
  echo "Starting scrub on $pool"
  zpool scrub "$pool" || {
    echo >&2 "Error: failed to start scrub on $pool"
    ((failed += 1))
  }
done

if ((failed != 0)); then
  echo >&2 "Error: Some pools could not be scrubbed"
  exit 1
fi
echo "Done"

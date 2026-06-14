#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

# Start a scrub on every imported pool. Scheduled monthly by the zfs_scrub timer
# (second Sunday), replacing the distro's /etc/cron.d/zfsutils-linux which the
# zfs role diverts aside. Iterating `zpool list` rather than a hand-maintained
# pool list means a newly-added pool is scrubbed automatically and no per-host
# config can drift.
#
# Deliberately no -w: a multi-TB pool scrubs for hours (lab tank ~8h, pug rpool
# ~18h), so a blocking oneshot would fight TimeoutStartSec and would also keep
# the unit "running" across the nightly zfs_autosnapshot window -- which pauses
# in-progress scrubs to avoid I/O contention (see zfs_autosnapshot). Kicking the
# scrub off and returning leaves that pause/resume free to operate. The outcome
# is watched out-of-band: zfs_health alarms on an expired (40-day) or canceled
# scrub, and ZED's scrub_finish zedlet mails on a scrub that finished with
# errors.
# Stagger the per-pool kick-offs. lab hard-locked ~12s into this run on
# 2026-06-14, during scrub initiation rather than steady state, so spacing the
# starts keeps every pool from entering its metadata-read burst in the same
# instant. A short gap (not full `-w` serialization -- deliberately avoided per
# the note above) leaves the unit non-blocking and finishes well before the
# nightly zfs_autosnapshot window. Skipped (already-scrubbing) pools don't
# consume a slot, so the first pool actually kicked off waits for nothing.
scrub_started=0
for pool in $(zpool list -H -o name); do
  # Skip a pool already scrubbing or resilvering: a fresh `zpool scrub` would
  # error out, and a long scrub spanning two monthly fires must not be
  # restarted from zero.
  if zpool status "$pool" | grep -q -e "scrub in progress" -e "resilver in progress"; then
    echo "Scrub/resilver already running on $pool, skipping."
    continue
  fi
  if [ "$scrub_started" -ne 0 ]; then
    sleep 120
  fi
  scrub_started=1
  echo "Starting scrub on $pool"
  zpool scrub "$pool"
done

echo "Done"

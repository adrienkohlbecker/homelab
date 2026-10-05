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
# zpool owns the per-pool scan state: it resumes a paused scrub and refuses to
# start one while a scrub or resilver is running, so a long scan is never
# restarted from zero. A refusal counts as a failure here; it takes a resilver
# or a scrub outlasting a month, both worth a look.
#
# Stagger the per-pool kick-offs. lab hard-locked ~12s into this run on
# 2026-06-14, during scrub initiation rather than steady state, so spacing the
# starts keeps every pool from entering its metadata-read burst in the same
# instant. The stagger is overridable (ZFS_SCRUB_STAGGER_SEC) so the role's
# _verify can zero it -- the CI fixture's img-backed pools can't
# thundering-herd a hard lock.
stagger_sec="${ZFS_SCRUB_STAGGER_SEC:-120}"
scrub_started=0
failed=0
pools=$(timeout -k 10 60 zpool list -H -o name)
for pool in $pools; do
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

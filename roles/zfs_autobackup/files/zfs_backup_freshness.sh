#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

# Data-level dead-man check for the backup mesh. The nightly legs already
# page on ERROR (a failed leg fails zfs_autosnapshot.service, which the
# netdata systemd-units alert reports), but a dataset can also fall out of
# the mesh SILENTLY -- a restore leaving autobackup:bak at source `received`
# (the -s local snapshot picker skips it without erroring), or a peer
# replica that quietly stopped receiving. So check the DATA: every
# filesystem tagged autobackup:bak=true -- any source, which covers this
# host's own datasets AND the replica trees it receives (tank/<host> on
# lab, apoc/lab on pug) -- must carry a @bak- snapshot younger than
# MAX_AGE_HOURS. Failing here fails the timer unit, which pages the same
# way the nightly does. 48h absorbs one slow nightly (the 02:00 run may
# legitimately still be transferring when this fires) without letting a
# second one pass unnoticed.
MAX_AGE_HOURS=48

now=$(date +%s)
failed=0
while IFS=$'\t' read -r ds value; do
  if [ "$value" != true ]; then
    continue
  fi
  newest=$(zfs list -t snapshot -H -p -o name,creation -s creation "$ds" | awk -F '\t' '$1 ~ /@bak-/ {c = $2} END {print c}')
  if [ -z "$newest" ]; then
    echo >&2 "STALE: $ds carries autobackup:bak=true but has no @bak- snapshot"
    failed=1
  elif ((now - newest > MAX_AGE_HOURS * 3600)); then
    echo >&2 "STALE: $ds newest @bak- snapshot is $(((now - newest) / 3600))h old (limit ${MAX_AGE_HOURS}h)"
    failed=1
  fi
done < <(zfs get -t filesystem -H -o name,value autobackup:bak)

if ((failed)); then
  echo >&2 "Error: some backed-up datasets have stale or missing @bak- snapshots"
  exit 1
fi

echo "All autobackup:bak datasets carry a @bak- snapshot fresher than ${MAX_AGE_HOURS}h"

#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

# Data-level dead-man check for the backup mesh. The nightly legs already
# page on ERROR (a failed leg fails zfs_autosnapshot.service, which the
# netdata systemd-units alert reports), but a dataset can also fall out of
# the mesh SILENTLY -- a restore leaving autobackup:bak at source `received`
# (the -s local snapshot picker skips it without erroring), or a peer
# replica that quietly stopped receiving. So check the DATA: every
# filesystem tagged autobackup:bak=true -- any source, which covers this
# host's own datasets AND the replica trees it receives (tank/<host> on
# lab, apoc/lab on pug) -- must carry a @bak- snapshot younger than
# MAX_AGE_SECONDS. Failing here fails the timer unit, which pages the same
# way the nightly does. 48h absorbs one slow nightly (the 02:00 run may
# legitimately still be transferring when this fires) without letting a
# second one pass unnoticed.
#
# To RETIRE a replica deliberately (a pre-rebuild rpool/ROOT/<release>
# whose source dataset is gone and will never advance again), set a local
# override on the holder: `zfs set autobackup:bak=false <replica>`. The
# local value wins over the received one, the value filter below skips it,
# and the onsite pull no longer touches the dataset so the override sticks.
# Destroy the replica once its rollback window closes.
MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-172800}
if [[ ! $MAX_AGE_SECONDS =~ ^[0-9]+$ ]]; then
  echo >&2 "Error: MAX_AGE_SECONDS must be a non-negative integer"
  exit 2
fi
NOW_SECONDS=${NOW_SECONDS:-$(date +%s)}
if [[ ! $NOW_SECONDS =~ ^[0-9]+$ ]]; then
  echo >&2 "Error: NOW_SECONDS must be a non-negative integer"
  exit 2
fi

failed=0
checked=0
if ! dataset_rows=$(zfs get -t filesystem -H -o name,value autobackup:bak); then
  echo >&2 "Error: failed to enumerate autobackup:bak datasets"
  exit 1
fi
while IFS=$'\t' read -r ds value; do
  if [ "$value" != true ]; then
    continue
  fi
  ((checked += 1))
  newest=$(zfs list -t snapshot -H -p -o name,creation -s creation "$ds" | awk -F '\t' '$1 ~ /@bak-/ {c = $2} END {print c}')
  if [ -z "$newest" ]; then
    echo >&2 "STALE: $ds carries autobackup:bak=true but has no @bak- snapshot"
    failed=1
  elif ((NOW_SECONDS - newest > MAX_AGE_SECONDS)); then
    echo >&2 "STALE: $ds newest @bak- snapshot is $((NOW_SECONDS - newest))s old (limit ${MAX_AGE_SECONDS}s)"
    failed=1
  fi
done <<<"$dataset_rows"

if ((checked == 0)); then
  echo >&2 "Error: no datasets have autobackup:bak=true"
  exit 1
fi

if ((failed)); then
  echo >&2 "Error: some backed-up datasets have stale or missing @bak- snapshots"
  exit 1
fi

echo "All $checked autobackup:bak datasets carry a @bak- snapshot fresher than ${MAX_AGE_SECONDS}s"

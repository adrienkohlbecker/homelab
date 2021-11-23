#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

/usr/local/bin/zfs-autobackup \
  --allow-empty \
  --buffer 1G \
  --clear-mountpoint \
  --clear-refreservation \
  --keep-source 10,1d1w,1w1m,1m10y \
  --keep-target 10,1d1w,1w1m,1m10y \
  --post-snapshot-cmd 'systemctl start compose' \
  --pre-snapshot-cmd 'systemctl stop compose' \
  --pre-snapshot-cmd 'timeout 300 wait-for-compose' \
  --set-properties readonly=on \
  --verbose \
  --zfs-compressed \
  bak backup

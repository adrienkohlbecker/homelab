#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

zfs-autobackup \
  --buffer 256M \
  --no-snapshot \
  --exclude-received \
  --clear-mountpoint \
  --clear-refreservation \
  --keep-source 16384 \
  --keep-target 10,1d1w,1w1m,1m10y \
  --set-properties readonly=on \
  --verbose \
  bak bak/backup

zfs-autobackup \
  --buffer 256M \
  --no-snapshot \
  --exclude-received \
  --clear-mountpoint \
  --clear-refreservation \
  --keep-source 16384 \
  --keep-target 10,1d1w,1w1m,1m10y \
  --set-properties readonly=on \
  --verbose \
  --ssh-config /root/.ssh/zfs_autobackup.conf \
  --ssh-source zfs_autobackup@10.123.0.11 \
  --compress=zstd-fast \
  bak bak/homelab

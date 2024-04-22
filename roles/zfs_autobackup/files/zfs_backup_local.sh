#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

zfs-autobackup \
  --no-send \
  --keep-source 10,1d1w,1w1m,1m10y \
  --keep-target 16384 \
  --allow-empty \
  --exclude-received \
  --post-snapshot-cmd 'systemctl start zfs_autosnapshot.target' \
  --pre-snapshot-cmd 'systemctl stop zfs_autosnapshot.target' \
  --verbose \
  bak

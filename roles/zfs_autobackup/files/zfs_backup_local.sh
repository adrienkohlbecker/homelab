#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

f_trace zfs-autobackup \
  --no-send \
  --keep-source 10,1d1w,1w1m,1m10y \
  --keep-target 16384 \
  --allow-empty \
  --exclude-received \
  --post-snapshot-cmd 'systemctl start zfs_autosnapshot.target' \
  --pre-snapshot-cmd 'systemctl stop zfs_autosnapshot.target' \
  --verbose \
  bak

#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

zfs-autobackup \
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

LAST_SNAPSHOT=$(zfs list -t snapshot -o name -s creation -r data/data | grep "@bak-" | tail -1 | cut -d@ -f2)
RSYNC=(rsync -ah --delete --delete-excluded -e "ssh -i /home/ak/.ssh/id_rsa" --one-file-system --exclude .DS_Store --exclude "._*" --exclude .DocumentRevisions-V100 --exclude .Trashes --exclude .TemporaryItems)
BUNK=ak@10.123.60.5

"${RSYNC[@]}" "/mnt/data/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/data
"${RSYNC[@]}" "/mnt/services/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/services
"${RSYNC[@]}" "/mnt/vms/ssd/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/vms_ssd
"${RSYNC[@]}" "/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/rpool
"${RSYNC[@]}" "/mnt/arq/adrien/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/arq/adrien
"${RSYNC[@]}" "/mnt/arq/marie/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/arq/marie
"${RSYNC[@]}" "/mnt/brumath/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/brumath
"${RSYNC[@]}" "/mnt/eckwersheim/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/eckwersheim

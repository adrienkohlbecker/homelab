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

# {% if inventory_hostname == 'beelink' -%}
# DATASET=beelink/arq/adrien
# {% else -%}
# DATASET=data/data
# {% endif %}

# LAST_SNAPSHOT=$(zfs list -t snapshot -o name -s creation -r $DATASET | grep "@bak-" | tail -1 | cut -d@ -f2)
# RSYNC=(rsync -ah --delete --delete-excluded -e "ssh -i /home/ak/.ssh/id_rsa" --one-file-system --exclude .DS_Store --exclude "._*" --exclude .DocumentRevisions-V100 --exclude .Trashes --exclude .TemporaryItems)
# BUNK=ak@10.123.60.5

# {% if inventory_hostname == 'beelink' -%}
# "${RSYNC[@]}" "/mnt/arq/adrien/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/arq/adrien
# "${RSYNC[@]}" "/mnt/arq/marie/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/arq/marie
# "${RSYNC[@]}" "/mnt/arq/game/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/arq/game
# "${RSYNC[@]}" "/mnt/services/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/services_beelink
# {% else -%}
# "${RSYNC[@]}" "/mnt/data/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/data
# "${RSYNC[@]}" "/mnt/services/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/services
# "${RSYNC[@]}" "/mnt/vms/ssd/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/vms_ssd
# "${RSYNC[@]}" "/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/rpool
# "${RSYNC[@]}" "/mnt/brumath/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/brumath
# "${RSYNC[@]}" "/mnt/eckwersheim/.zfs/snapshot/$LAST_SNAPSHOT/" $BUNK:/volume1/Backup/eckwersheim
# {% endif %}

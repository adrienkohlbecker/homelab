#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eu
set -o pipefail
IFS=$'\n\t'

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root" 1>&2
   exit 1
fi

COMMAND="$1"
shift

case "$COMMAND" in

  snapshot)

    NAME="backup-$(date +%Y%m%d-%H%M%S)"

    echo "Snapshotting with name=$NAME"

    zfs snapshot "rpool/ROOT/xenial@$NAME"
    zfs snapshot "rpool/docker@$NAME"
    zfs snapshot "rpool/vms@$NAME"
    zfs snapshot "tank/vms@$NAME"
    zfs snapshot "tank/legacy@$NAME"
    zfs snapshot "tank/pictures@$NAME"
    zfs snapshot "tank/brumath@$NAME"
    zfs snapshot "tank/videos@$NAME"
    zfs snapshot "tank/sftp@$NAME"

    ;;

  backup)

    DISK="$1"
    FROM="$2"

    echo "Destroying snapshots: backup-disk-$DISK"

    zfs destroy -f "rpool/ROOT/xenial@backup-disk-$DISK"
    zfs destroy -f "rpool/docker@backup-disk-$DISK"
    zfs destroy -f "rpool/vms@backup-disk-$DISK"
    zfs destroy -f "tank/vms@backup-disk-$DISK"
    zfs destroy -f "tank/legacy@backup-disk-$DISK"
    zfs destroy -f "tank/pictures@backup-disk-$DISK"
    zfs destroy -f "tank/brumath@backup-disk-$DISK"
    zfs destroy -f "tank/videos@backup-disk-$DISK"
    zfs destroy -f "tank/sftp@backup-disk-$DISK"

    zfs destroy -f "backup-$DISK/xenial@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/docker@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/vms_ssd@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/vms_hdd@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/legacy@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/pictures@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/brumath@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/videos@backup-disk-$DISK"
    zfs destroy -f "backup-$DISK/sftp@backup-disk-$DISK"

    echo "Rolling back backup to: $FROM"

    zfs rollback "backup-$DISK/xenial@$FROM"
    zfs rollback "backup-$DISK/docker@$FROM"
    zfs rollback "backup-$DISK/vms_ssd@$FROM"
    zfs rollback "backup-$DISK/vms_hdd@$FROM"
    zfs rollback "backup-$DISK/legacy@$FROM"
    zfs rollback "backup-$DISK/pictures@$FROM"
    zfs rollback "backup-$DISK/sftp@$FROM"
    zfs rollback "backup-$DISK/brumath@$FROM"
    zfs rollback "backup-$DISK/videos@$FROM"

    echo "Snapshotting with name=backup-disk-$DISK"

    zfs snapshot "rpool/ROOT/xenial@backup-disk-$DISK"
    zfs snapshot "rpool/docker@backup-disk-$DISK"
    zfs snapshot "rpool/vms@backup-disk-$DISK"
    zfs snapshot "tank/vms@backup-disk-$DISK"
    zfs snapshot "tank/legacy@backup-disk-$DISK"
    zfs snapshot "tank/pictures@backup-disk-$DISK"
    zfs snapshot "tank/brumath@backup-disk-$DISK"
    zfs snapshot "tank/videos@backup-disk-$DISK"
    zfs snapshot "tank/sftp@backup-disk-$DISK"

    echo "Incremental sync to backup: $FROM -> backup-disk-$DISK"

    zfs send -pv -I "rpool/ROOT/xenial@$FROM" "rpool/ROOT/xenial@backup-disk-$DISK" | zfs receive -v "backup-$DISK/xenial"
    zfs send -pv -I "rpool/docker@$FROM" "rpool/docker@backup-disk-$DISK" | zfs receive -v "backup-$DISK/docker"
    zfs send -pv -I "rpool/vms@$FROM" "rpool/vms@backup-disk-$DISK" | zfs receive -v "backup-$DISK/vms_ssd"
    zfs send -pv -I "tank/vms@$FROM" "tank/vms@backup-disk-$DISK" | zfs receive -v "backup-$DISK/vms_hdd"
    zfs send -pv -I "tank/legacy@$FROM" "tank/legacy@backup-disk-$DISK" | zfs receive -v "backup-$DISK/legacy"
    zfs send -pv -I "tank/pictures@$FROM" "tank/pictures@backup-disk-$DISK" | zfs receive -v "backup-$DISK/pictures"
    zfs send -pv -I "tank/brumath@$FROM" "tank/brumath@backup-disk-$DISK" | zfs receive -v "backup-$DISK/brumath"
    zfs send -pv -I "tank/videos@$FROM" "tank/videos@backup-disk-$DISK" | zfs receive -v "backup-$DISK/videos"
    zfs send -pv -I "tank/sftp@$FROM" "tank/sftp@backup-disk-$DISK" | zfs receive -v "backup-$DISK/sftp"

    ;;

  destroy)

    SNAPSHOT="$1"

    echo "Destroying snapshots: $SNAPSHOT"

    zfs destroy "rpool/ROOT/xenial@$SNAPSHOT"
    zfs destroy "rpool/docker@$SNAPSHOT"
    zfs destroy "rpool/vms@$SNAPSHOT"
    zfs destroy "tank/vms@$SNAPSHOT"
    zfs destroy "tank/legacy@$SNAPSHOT"
    zfs destroy "tank/pictures@$SNAPSHOT"
    zfs destroy "tank/brumath@$SNAPSHOT"
    zfs destroy "tank/videos@$SNAPSHOT"
    zfs destroy "tank/sftp@$SNAPSHOT"

    ;;

esac

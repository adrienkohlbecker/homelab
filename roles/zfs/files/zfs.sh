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

    zfs snapshot "rpool/ROOT/ubuntu-1@$NAME"
    zfs snapshot "rpool/vms@$NAME"
    zfs snapshot "tank/vms@$NAME"
    zfs snapshot "tank/legacy@$NAME"
    zfs snapshot "tank/pictures@$NAME"

    ;;

  backup)

    FROM="$1"
    TO="$2"

    echo "Rolling back backup to: $FROM"

    zfs rollback "backup/ubuntu@$FROM"
    zfs rollback "backup/vms_ssd@$FROM"
    zfs rollback "backup/vms_hdd@$FROM"
    zfs rollback "backup/legacy@$FROM"
    zfs rollback "backup/pictures@$FROM"

    echo "Incremental sync to backup: $FROM -> $TO"

    zfs send -pv -I "rpool/ROOT/ubuntu-1@$FROM" "rpool/ROOT/ubuntu-1@$TO" | zfs receive -v backup/ubuntu
    zfs send -pv -I "rpool/vms@$FROM" "rpool/vms@$TO" | zfs receive -v backup/vms_ssd
    zfs send -pv -I "tank/vms@$FROM" "tank/vms@$TO" | zfs receive -v backup/vms_hdd
    zfs send -pv -I "tank/legacy@$FROM" "tank/legacy@$TO" | zfs receive -v backup/legacy
    zfs send -pv -I "tank/pictures@$FROM" "tank/pictures@$TO" | zfs receive -v backup/pictures

    ;;

  destroy)

    SNAPSHOT="$1"

    echo "Destroying snapshots: $SNAPSHOT"

    zfs destroy "rpool/ROOT/ubuntu-1@$SNAPSHOT"
    zfs destroy "rpool/vms@$SNAPSHOT"
    zfs destroy "tank/vms@$SNAPSHOT"
    zfs destroy "tank/legacy@$SNAPSHOT"
    zfs destroy "tank/pictures@$SNAPSHOT"

    ;;

esac

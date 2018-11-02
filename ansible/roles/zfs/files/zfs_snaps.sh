#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

COMMAND="$1"
shift

case "$COMMAND" in

  snapshot)

    NAME="backup-$(date +%Y%m%d-%H%M%S)"

    echo "Snapshotting with name=$NAME"

    zfs snapshot "rpool/ROOT/bionic@$NAME"
    zfs snapshot "rpool/services@$NAME"
    zfs snapshot "tank/legacy@$NAME"
    zfs snapshot "tank/pictures@$NAME"
    zfs snapshot "tank/brumath@$NAME"
    zfs snapshot "tank/videos@$NAME"
    zfs snapshot "tank/arq@$NAME"

    ;;

  backup)

    DISK="$1"
    FROM="$2"

    echo "Destroying snapshots: backup-disk-$DISK"

    zfs destroy -f "rpool/ROOT/bionic@backup-disk-$DISK" || true
    zfs destroy -f "rpool/services@backup-disk-$DISK"    || true
    zfs destroy -f "tank/legacy@backup-disk-$DISK"       || true
    zfs destroy -f "tank/pictures@backup-disk-$DISK"     || true
    zfs destroy -f "tank/brumath@backup-disk-$DISK"      || true
    zfs destroy -f "tank/videos@backup-disk-$DISK"       || true
    zfs destroy -f "tank/arq@backup-disk-$DISK"          || true

    zfs destroy -f "backup-$DISK/bionic@backup-disk-$DISK"   || true
    zfs destroy -f "backup-$DISK/services@backup-disk-$DISK" || true
    zfs destroy -f "backup-$DISK/legacy@backup-disk-$DISK"   || true
    zfs destroy -f "backup-$DISK/pictures@backup-disk-$DISK" || true
    zfs destroy -f "backup-$DISK/brumath@backup-disk-$DISK"  || true
    zfs destroy -f "backup-$DISK/videos@backup-disk-$DISK"   || true
    zfs destroy -f "backup-$DISK/arq@backup-disk-$DISK"     || true

    echo "Rolling back backup to: $FROM"

    zfs rollback -r "backup-$DISK/bionic@$FROM"
    zfs rollback -r "backup-$DISK/services@$FROM"
    zfs rollback -r "backup-$DISK/legacy@$FROM"
    zfs rollback -r "backup-$DISK/pictures@$FROM"
    zfs rollback -r "backup-$DISK/arq@$FROM"
    zfs rollback -r "backup-$DISK/brumath@$FROM"
    zfs rollback -r "backup-$DISK/videos@$FROM"

    echo "Snapshotting with name=backup-disk-$DISK"

    zfs snapshot "rpool/ROOT/bionic@backup-disk-$DISK"
    zfs snapshot "rpool/services@backup-disk-$DISK"
    zfs snapshot "tank/legacy@backup-disk-$DISK"
    zfs snapshot "tank/pictures@backup-disk-$DISK"
    zfs snapshot "tank/brumath@backup-disk-$DISK"
    zfs snapshot "tank/videos@backup-disk-$DISK"
    zfs snapshot "tank/arq@backup-disk-$DISK"

    echo "Incremental sync to backup: $FROM -> backup-disk-$DISK"

    zfs send -pv -I "rpool/ROOT/bionic@$FROM" "rpool/ROOT/bionic@backup-disk-$DISK" | zfs receive -v "backup-$DISK/bionic"
    zfs send -pv -I "rpool/services@$FROM" "rpool/services@backup-disk-$DISK" | zfs receive -v "backup-$DISK/services"
    zfs send -pv -I "tank/legacy@$FROM" "tank/legacy@backup-disk-$DISK" | zfs receive -v "backup-$DISK/legacy"
    zfs send -pv -I "tank/pictures@$FROM" "tank/pictures@backup-disk-$DISK" | zfs receive -v "backup-$DISK/pictures"
    zfs send -pv -I "tank/brumath@$FROM" "tank/brumath@backup-disk-$DISK" | zfs receive -v "backup-$DISK/brumath"
    zfs send -pv -I "tank/videos@$FROM" "tank/videos@backup-disk-$DISK" | zfs receive -v "backup-$DISK/videos"
    zfs send -pv -I "tank/arq@$FROM" "tank/arq@backup-disk-$DISK" | zfs receive -v "backup-$DISK/arq"

    ;;

  destroy)

    SNAPSHOT="$1"

    echo "Destroying snapshots: $SNAPSHOT"

    zfs destroy "rpool/ROOT/bionic@$SNAPSHOT"
    zfs destroy "rpool/services@$SNAPSHOT"
    zfs destroy "tank/legacy@$SNAPSHOT"
    zfs destroy "tank/pictures@$SNAPSHOT"
    zfs destroy "tank/brumath@$SNAPSHOT"
    zfs destroy "tank/videos@$SNAPSHOT"
    zfs destroy "tank/arq@$SNAPSHOT"

    ;;

  destroy-backup)

    DISK="$1"
    SNAPSHOT="$2"

    echo "Destroying snapshots: $SNAPSHOT"

    zfs destroy -f "backup-$DISK/bionic@$SNAPSHOT"   || true
    zfs destroy -f "backup-$DISK/services@$SNAPSHOT" || true
    zfs destroy -f "backup-$DISK/legacy@$SNAPSHOT"   || true
    zfs destroy -f "backup-$DISK/pictures@$SNAPSHOT" || true
    zfs destroy -f "backup-$DISK/brumath@$SNAPSHOT"  || true
    zfs destroy -f "backup-$DISK/videos@$SNAPSHOT"   || true
    zfs destroy -f "backup-$DISK/arq@$SNAPSHOT"     || true

    ;;

esac

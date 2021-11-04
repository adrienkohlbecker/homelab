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
    zfs snapshot "rpool/vms@$NAME"
    zfs snapshot "data/data@$NAME"
    zfs snapshot "data/brumath@$NAME"
    zfs snapshot "data/eckwersheim@$NAME"
    zfs snapshot "data/arq/adrien@$NAME"
    zfs snapshot "data/arq/marie@$NAME"

    ;;

  backup)

    FROM="$1"

    echo "Destroying snapshots: backup"

    zfs destroy -f "rpool/ROOT/bionic@backup" || true
    zfs destroy -f "rpool/services@backup"    || true
    zfs destroy -f "rpool/vms@backup"         || true
    zfs destroy -f "data/data@backup"         || true
    zfs destroy -f "data/brumath@backup"      || true
    zfs destroy -f "data/eckwersheim@backup"  || true
    zfs destroy -f "data/arq/adrien@backup"   || true
    zfs destroy -f "data/arq/marie@backup"    || true

    zfs destroy -f "backup/bionic@backup"       || true
    zfs destroy -f "backup/services@backup"     || true
    zfs destroy -f "backup/vms_ssd@backup"      || true
    zfs destroy -f "backup/data@backup"         || true
    zfs destroy -f "backup/brumath@backup"      || true
    zfs destroy -f "backup/eckwersheim@backup"  || true
    zfs destroy -f "backup/arq_adrien@backup"   || true
    zfs destroy -f "backup/arq_marie@backup"    || true

    echo "Rolling back backup to: $FROM"

    zfs rollback -r "backup/bionic@$FROM"
    zfs rollback -r "backup/services@$FROM"
    zfs rollback -r "backup/vms_ssd@$FROM"
    zfs rollback -r "backup/data@$FROM"
    zfs rollback -r "backup/arq_adrien@$FROM"
    zfs rollback -r "backup/arq_marie@$FROM"
    zfs rollback -r "backup/brumath@$FROM"
    zfs rollback -r "backup/eckwersheim@$FROM"

    echo "Snapshotting with name=backup"

    zfs snapshot "rpool/ROOT/bionic@backup"
    zfs snapshot "rpool/services@backup"
    zfs snapshot "rpool/vms@backup"
    zfs snapshot "data/data@backup"
    zfs snapshot "data/brumath@backup"
    zfs snapshot "data/eckwersheim@backup"
    zfs snapshot "data/arq/adrien@backup"
    zfs snapshot "data/arq/marie@backup"

    echo "Incremental sync to backup: $FROM -> backup"

    zfs send -pv -I "rpool/ROOT/bionic@$FROM" "rpool/ROOT/bionic@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/bionic"
    zfs send -pv -I "rpool/services@$FROM" "rpool/services@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/services"
    zfs send -pv -I "rpool/vms@$FROM" "rpool/vms@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/vms_ssd"
    zfs send -pv -I "data/data@$FROM" "data/data@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/data"
    zfs send -pv -I "data/brumath@$FROM" "data/brumath@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/brumath"
    zfs send -pv -I "data/eckwersheim@$FROM" "data/eckwersheim@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/eckwersheim"
    zfs send -pv -I "data/arq/adrien@$FROM" "data/arq/adrien@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/arq_adrien"
    zfs send -pv -I "data/arq/marie@$FROM" "data/arq/marie@backup" | mbuffer -q -s 128k -m 1G | zfs receive -v "backup/arq_marie"

    ;;

  destroy)

    SNAPSHOT="$1"

    echo "Destroying snapshots: $SNAPSHOT"

    # legacy
    zfs destroy "data/pictures@$SNAPSHOT"      || true
    zfs destroy "data/videos@$SNAPSHOT"        || true

    zfs destroy "rpool/ROOT/bionic@$SNAPSHOT"  || true
    zfs destroy "rpool/services@$SNAPSHOT"     || true
    zfs destroy "rpool/vms@$SNAPSHOT"          || true
    zfs destroy "data/data@$SNAPSHOT"          || true
    zfs destroy "data/brumath@$SNAPSHOT"       || true
    zfs destroy "data/eckwersheim@$SNAPSHOT"   || true
    zfs destroy "data/arq/adrien@$SNAPSHOT"    || true
    zfs destroy "data/arq/marie@$SNAPSHOT"     || true

    ;;

  destroy-backup)

    SNAPSHOT="$1"

    echo "Destroying snapshots: $SNAPSHOT"

    # legacy
    zfs destroy -f "backup/pictures@$SNAPSHOT"     || true
    zfs destroy -f "backup/videos@$SNAPSHOT"       || true

    zfs destroy -f "backup/bionic@$SNAPSHOT"       || true
    zfs destroy -f "backup/services@$SNAPSHOT"     || true
    zfs destroy -f "backup/vms_ssd@$SNAPSHOT"      || true
    zfs destroy -f "backup/data@$SNAPSHOT"         || true
    zfs destroy -f "backup/brumath@$SNAPSHOT"      || true
    zfs destroy -f "backup/eckwersheim@$SNAPSHOT"  || true
    zfs destroy -f "backup/arq_adrien@$SNAPSHOT"   || true
    zfs destroy -f "backup/arq_marie@$SNAPSHOT"    || true

    ;;

esac

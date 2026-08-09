#!/bin/bash
# Temporary lab migration support. Source from provision.sh only when the
# existing tank-special p6 members must survive an rpool rebuild.
set -euo pipefail

# Exact marker checked by the lab rebuild runbook before the live installer is
# allowed to touch the three rpool disks.
# shellcheck disable=SC2034  # consumed by the runbook's source-integrity gate
PRESERVE_META_CONTRACT=1

preserve_meta_preflight() {
  local disks_count extra_disks_count

  case $PRESERVE_META in true | false) ;; *)
    echo "preserve_meta.sh: PRESERVE_META must be true or false (got '$PRESERVE_META')" >&2
    return 1
    ;;
  esac
  case $PRESERVE_META_FIXTURE in true | false) ;; *)
    echo "preserve_meta.sh: PRESERVE_META_FIXTURE must be true or false (got '$PRESERVE_META_FIXTURE')" >&2
    return 1
    ;;
  esac

  if [ "$PRESERVE_META" = true ]; then
    disks_count=$(wc -w <<<"$DISKS")
    extra_disks_count=$(wc -w <<<"$EXTRA_DISKS")
    if [ "$LAYOUT" != mirror ] || [ "$disks_count" -ne 3 ]; then
      echo "preserve_meta.sh: PRESERVE_META requires LAYOUT=mirror and exactly three DISKS" >&2
      return 1
    fi
    if [ "$META_SIZE" != 128G ] || [ -z "$PODMAN_SIZE" ]; then
      echo "preserve_meta.sh: PRESERVE_META requires META_SIZE=128G and a non-empty PODMAN_SIZE" >&2
      return 1
    fi
    if [ -n "$EXTRA_POOLS" ]; then
      echo "preserve_meta.sh: PRESERVE_META forbids EXTRA_POOLS; existing pools must stay untouched" >&2
      return 1
    fi
    if [ "$PRESERVE_META_FIXTURE" = true ]; then
      if [ "${QEMU_TEST_IMAGE:-false}" != true ] || [ "${SOURCE_NAME:-}" != lab ] || [ "$extra_disks_count" -ne 6 ]; then
        echo "preserve_meta.sh: PRESERVE_META_FIXTURE is restricted to the six-disk qemu lab fixture" >&2
        return 1
      fi
    elif [ "$extra_disks_count" -ne 0 ]; then
      echo "preserve_meta.sh: production PRESERVE_META requires EXTRA_DISKS to be empty" >&2
      return 1
    fi
  elif [ "$PRESERVE_META_FIXTURE" = true ]; then
    echo "preserve_meta.sh: PRESERVE_META_FIXTURE requires PRESERVE_META=true" >&2
    return 1
  fi
}

PRESERVE_META_STATE_DIR=""
if [ "$PRESERVE_META_FIXTURE" = true ]; then
  # QEMU virtio exposes 512-byte logical sectors. Scale the live NVMe sector
  # numbers by eight so the fixture covers the identical 1013MiB start,
  # 128GiB payload, and 129GiB end.
  PRESERVE_META_SECTOR_SIZE=512
  PRESERVE_META_FIRST_SECTOR=2074624
  PRESERVE_META_LAST_SECTOR=270510079
  PRESERVE_META_SECTOR_COUNT=268435456
else
  PRESERVE_META_SECTOR_SIZE=4096
  PRESERVE_META_FIRST_SECTOR=259328
  PRESERVE_META_LAST_SECTOR=33813759
  PRESERVE_META_SECTOR_COUNT=33554432
fi

capture_preserved_meta() {
  local disk index part info zdb_out partition_guid pool_guid top_guid leaf_guid number
  local expected_pool_guid="" expected_top_guid=""
  local -A leaf_guids=()

  if zpool list -H -o name 2>/dev/null | grep -Eq '^(rpool|tank)$'; then
    echo "preserve_meta.sh: rpool and tank must both be exported before PRESERVE_META" >&2
    return 1
  fi

  PRESERVE_META_STATE_DIR=$(mktemp -d /tmp/preserve_meta.XXXXXX)
  index=0
  for disk in $DISKS; do
    index=$((index + 1))
    part=$(partdev "$disk" 6)
    info="$PRESERVE_META_STATE_DIR/$index.sgdisk"
    zdb_out="$PRESERVE_META_STATE_DIR/$index.zdb"

    if [ ! -b "$disk" ] || [ ! -b "$part" ]; then
      echo "preserve_meta.sh: expected whole disk '$disk' and preserved partition '$part'" >&2
      return 1
    fi
    if [ "$(blockdev --getss "$disk")" -ne "$PRESERVE_META_SECTOR_SIZE" ]; then
      echo "preserve_meta.sh: '$disk' has the wrong logical sector size for PRESERVE_META" >&2
      return 1
    fi

    number=0
    while read -r number; do
      if [ "$number" -gt 6 ]; then
        echo "preserve_meta.sh: unexpected partition $number on '$disk'; only p1-p6 are allowed" >&2
        return 1
      fi
    done < <(sgdisk -p "$disk" | awk '$1 ~ /^[0-9]+$/ { print $1 }')
    if [ "$(sgdisk -p "$disk" | awk '$1 == 6 { count++ } END { print count + 0 }')" -ne 1 ]; then
      echo "preserve_meta.sh: '$disk' must contain exactly one p6" >&2
      return 1
    fi

    sgdisk -i 6 "$disk" >"$info"
    grep -Fqx 'Partition GUID code: 6A898CC3-1DD2-11B2-99A6-080020736631 (Solaris /usr & Mac ZFS)' "$info"
    grep -Fqx "First sector: $PRESERVE_META_FIRST_SECTOR (at 1013.0 MiB)" "$info"
    grep -Fqx "Last sector: $PRESERVE_META_LAST_SECTOR (at 129.0 GiB)" "$info"
    grep -Fqx "Partition size: $PRESERVE_META_SECTOR_COUNT sectors (128.0 GiB)" "$info"
    grep -Fqx "Partition name: 'meta'" "$info"

    partition_guid=$(awk -F': ' '/^Partition unique GUID:/ { print $2 }' "$info")
    [[ $partition_guid =~ ^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$ ]] || {
      echo "preserve_meta.sh: invalid p6 partition GUID on '$disk': '$partition_guid'" >&2
      return 1
    }
    printf '%s\n' "$partition_guid" >"$PRESERVE_META_STATE_DIR/$index.partition_guid"

    zdb -l "$part" >"$zdb_out"
    grep -Fq "name: 'tank'" "$zdb_out"
    grep -Fq 'vdev_children: 2' "$zdb_out"
    grep -Fq "type: 'mirror'" "$zdb_out"
    grep -Fq 'children[2]:' "$zdb_out"
    if grep -Fq 'children[3]:' "$zdb_out"; then
      echo "preserve_meta.sh: tank p6 labels describe more than three mirror children" >&2
      return 1
    fi
    if [ "$(grep -c '^[[:space:]]*path:' "$zdb_out")" -ne 3 ]; then
      echo "preserve_meta.sh: tank p6 labels do not describe exactly three mirror leaves" >&2
      return 1
    fi

    pool_guid=$(awk '$1 == "pool_guid:" { print $2; exit }' "$zdb_out")
    top_guid=$(awk '$1 == "top_guid:" { print $2; exit }' "$zdb_out")
    leaf_guid=$(awk '$1 == "guid:" { print $2; exit }' "$zdb_out")
    if [ -z "$pool_guid" ] || [ -z "$top_guid" ] || [ -z "$leaf_guid" ]; then
      echo "preserve_meta.sh: incomplete tank ZFS label identity on '$part'" >&2
      return 1
    fi
    if [ -n "$expected_pool_guid" ] && { [ "$pool_guid" != "$expected_pool_guid" ] || [ "$top_guid" != "$expected_top_guid" ]; }; then
      echo "preserve_meta.sh: p6 devices do not belong to one tank special mirror" >&2
      return 1
    fi
    if [[ -v leaf_guids[$leaf_guid] ]]; then
      echo "preserve_meta.sh: duplicate tank leaf GUID '$leaf_guid'" >&2
      return 1
    fi
    expected_pool_guid=$pool_guid
    expected_top_guid=$top_guid
    leaf_guids[$leaf_guid]=1
  done
}

verify_preserved_meta() {
  local disk index part info zdb_out partition_guid

  index=0
  for disk in $DISKS; do
    index=$((index + 1))
    part=$(partdev "$disk" 6)
    info="$PRESERVE_META_STATE_DIR/$index.verify.sgdisk"
    zdb_out="$PRESERVE_META_STATE_DIR/$index.verify.zdb"

    [ -b "$part" ]
    sgdisk -i 6 "$disk" >"$info"
    partition_guid=$(awk -F': ' '/^Partition unique GUID:/ { print $2 }' "$info")
    [ "$partition_guid" = "$(<"$PRESERVE_META_STATE_DIR/$index.partition_guid")" ]
    cmp "$PRESERVE_META_STATE_DIR/$index.sgdisk" "$info"
    zdb -l "$part" >"$zdb_out"
    cmp "$PRESERVE_META_STATE_DIR/$index.zdb" "$zdb_out"
  done
}

partition_disk_preserving_meta() {
  local disk="$1" number part

  for number in 1 2 3 4 5; do
    part=$(partdev "$disk" "$number")
    [ -b "$part" ] || continue
    if [ "$number" -eq 4 ] || [ "$number" -eq 5 ]; then
      # Initial p4 and retry-created p5 may carry rpool labels; absence is the
      # expected benign case for the other layout position.
      zpool labelclear -f "$part" || true
    fi
    wipefs -a "$part"
    sgdisk --delete="$number" "$disk"
  done

  sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk"
  sgdisk -n2:1M:+1000M -t2:EF00 -c2:efi "$disk"
  sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk"
  sgdisk "-n4:0:+$PODMAN_SIZE" -t4:8300 -c4:podman "$disk"
  sgdisk -n5:0:0 -t5:BF00 -c5:rpool "$disk"
  sgdisk -p "$disk"
}

preserve_meta_partition_disks() {
  if [ "$PRESERVE_META_FIXTURE" = true ]; then
    "$SCRIPTS_DIR/preserve_meta_fixture.sh" seed
  fi

  # Validate and snapshot all three p6 identities before the first disk is
  # changed. A mismatch on disk three therefore cannot leave disks one and two
  # half-repartitioned.
  capture_preserved_meta
  partition_disks partition_disk_preserving_meta
  verify_preserved_meta

  if [ "$PRESERVE_META_FIXTURE" = true ]; then
    # Stamp the documented stopped retry state onto the first-pass layout, then
    # exercise the same production cleanup while comparing against the original
    # fixture labels and partition GUIDs.
    "$SCRIPTS_DIR/preserve_meta_fixture.sh" prepare_retry
    partition_disks partition_disk_preserving_meta
    verify_preserved_meta
  fi
}

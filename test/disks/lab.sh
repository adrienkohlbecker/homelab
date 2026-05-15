#!/bin/bash
set -euxo pipefail

# Create the dozer/tank/mouse pools on a lab-class test VM. Invoked by the
# test harness (via SSH+sudo) after the VM boots, before any role/_setup
# playbook runs. Idempotent per-pool: each zpool create is gated on a list
# check, so --keep re-runs work.
#
# Args (positional, in order matching what test/machine.py attaches):
#   $1, $2            — dozer mirror legs
#   $3, $4            — tank/mouse shared disks (partitioned for both pools
#                       plus a third "extra" partition kept for future use)
#   $5, $6            — additional tank raidz2 vdevs (whole-disk)

if [ "$#" -ne 6 ]; then
  echo >&2 "usage: $0 <dozer1> <dozer2> <tank-mouse1> <tank-mouse2> <tank3> <tank4>"
  exit 1
fi

DOZER1=$1
DOZER2=$2
TM1=$3
TM2=$4
TANK3=$5
TANK4=$6

if ! zpool list -H dozer >/dev/null 2>&1; then
  # exec=on (not the data-pool default exec=off) because github_runner's
  # actions/runner --work folder lives on dozer/scratch/github_runner/ and
  # CI workflows bind-mount that into containers that need to execve scripts
  # from the checkout. Bind-mounts inherit the source mount's noexec flag,
  # so pool-level exec=off propagates into the container as a hard EACCES
  # on `mise run ...` etc. setuid=off + devices=off stay for hardening.
  zpool create \
    -o ashift=12 \
    -o autotrim=on \
    -o compatibility=openzfs-2.1-linux \
    -O casesensitivity=insensitive \
    -O normalization=formD \
    -O utf8only=on \
    -O acltype=posix \
    -O atime=on \
    -O canmount=off \
    -O compression=zstd \
    -O devices=off \
    -O dnodesize=auto \
    -O overlay=off \
    -O relatime=on \
    -O setuid=off \
    -O xattr=sa \
    -m none \
    dozer mirror "$DOZER1" "$DOZER2"

  sync
  sleep 2
fi

for tm in "$TM1" "$TM2"; do
  # Partitioning is idempotent only if we skip when the table is already
  # populated; sgdisk -n on an existing partition errors out otherwise.
  if ! sgdisk -p "$tm" 2>/dev/null | grep -q "^   1 "; then
    sgdisk -n1:0:+1014M -t1:BF01 "$tm" # tank
    sgdisk -n2:0:-8M -t2:BF01 "$tm"    # mouse
    sgdisk -n3:0:0 -t3:BF07 "$tm"      # extra
    sgdisk -p "$tm"
  fi
done

sync
sleep 2

if ! zpool list -H tank >/dev/null 2>&1; then
  # See dozer block above for why exec=off is dropped.
  zpool create \
    -o ashift=12 \
    -o autotrim=off \
    -o compatibility=openzfs-2.1-linux \
    -O casesensitivity=insensitive \
    -O normalization=formD \
    -O utf8only=on \
    -O acltype=posix \
    -O atime=on \
    -O canmount=off \
    -O compression=zstd \
    -O devices=off \
    -O dnodesize=auto \
    -O overlay=off \
    -O relatime=on \
    -O setuid=off \
    -O xattr=sa \
    -m none \
    tank raidz2 "${TM1}1" "${TM2}1" "$TANK3" "$TANK4"

  sync
  sleep 2
fi

if ! zpool list -H mouse >/dev/null 2>&1; then
  # See dozer block above for why exec=off is dropped.
  zpool create \
    -o ashift=12 \
    -o autotrim=off \
    -o compatibility=openzfs-2.1-linux \
    -O casesensitivity=insensitive \
    -O normalization=formD \
    -O utf8only=on \
    -O acltype=posix \
    -O atime=on \
    -O canmount=off \
    -O compression=zstd \
    -O devices=off \
    -O dnodesize=auto \
    -O overlay=off \
    -O relatime=on \
    -O setuid=off \
    -O xattr=sa \
    -m none \
    mouse mirror "${TM1}2" "${TM2}2"

  sync
  sleep 2
fi

#!/bin/bash
set -euxo pipefail

# Create the apoc mirror pool on a pug-class test VM. Invoked by the test
# harness (via SSH+sudo) after the VM boots, before any role/_setup playbook
# runs. Idempotent: skips when the pool already exists, so --keep re-runs of
# testrole.py work without recreating state.

if [ "$#" -ne 2 ]; then
  echo >&2 "usage: $0 <disk1> <disk2>"
  exit 1
fi

if zpool list -H apoc >/dev/null 2>&1; then
  echo "Pool 'apoc' already exists, skipping"
  exit 0
fi

# exec=off intentionally omitted (default exec=on): bind-mounts of
# pool-resident paths into rootless containers inherit the source
# mount's noexec flag, which surfaces as EACCES on execve from the
# checkout (see test/disks/lab.sh for the github_runner-on-dozer
# precedent). setuid=off + devices=off stay for hardening.
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
  apoc mirror "$1" "$2"

sync
sleep 2

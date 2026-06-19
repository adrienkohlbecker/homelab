#!/usr/bin/env bash
# Prepare the CI qemu host scratch area on local instance-store NVMe.
#
# c8id and friends expose one or more ephemeral NVMe disks (model
# "Amazon EC2 NVMe Instance Storage"). RAID0 them into a single fast volume and
# mount it at /mnt/scratch so every heavy writer lives off the small EBS root:
# the qcow2 overlays under homelab_ci/ and the gitlab-runner checkout, cache,
# and build tree under gitlab-runner/. Instance store is physically wiped on
# stop/terminate, which is exactly right for ephemeral CI scratch.
#
# Falls back to the EBS root filesystem when no instance store is present (the
# c6a packer build host has none), so the bake's own boot still succeeds. The
# prod pool is pinned to c8id, so there a missing or failed NVMe mount fails
# this unit and homelab_ci_ready then rejects the host.
set -euo pipefail

mountpoint=/mnt/scratch

if ! mountpoint -q "$mountpoint"; then
  mapfile -t devs < <(
    lsblk -dn -o NAME,MODEL | awk '/Instance Storage/ { print "/dev/" $1 }'
  )
  if [ "${#devs[@]}" -gt 0 ]; then
    if [ "${#devs[@]}" -gt 1 ]; then
      mdadm --create /dev/md0 --level=0 --force --run \
        --raid-devices="${#devs[@]}" "${devs[@]}"
      target=/dev/md0
    else
      target="${devs[0]}"
    fi
    mkfs.ext4 -F -L homelab_ci_scratch "$target"
    mkdir -p "$mountpoint"
    mount -o noatime "$target" "$mountpoint"
  else
    mkdir -p "$mountpoint"
  fi
fi

# gitlab-runner (instance executor, ssh user ubuntu) checkout + cache + builds.
install -dm 0755 -o ubuntu -g ubuntu \
  "$mountpoint/gitlab-runner" \
  "$mountpoint/gitlab-runner/builds" \
  "$mountpoint/gitlab-runner/cache"
# qemu scratch, created last so homelab_ci_ready's writable check on it implies
# the whole tree is staged.
install -dm 0755 -o ubuntu -g ubuntu "$mountpoint/homelab_ci"

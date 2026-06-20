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
# Created before the swap cushion below: homelab_ci_ready gates on these being
# writable, and fleeting reaps a host whose instance-ready command keeps failing,
# so nothing slow may sit between the mount and these dirs.
install -dm 0755 -o ubuntu -g ubuntu \
  "$mountpoint/gitlab-runner" \
  "$mountpoint/gitlab-runner/builds" \
  "$mountpoint/gitlab-runner/cache"
# qemu scratch, created last of the readiness dirs so homelab_ci_ready's writable
# check on it implies the whole tree is staged.
install -dm 0755 -o ubuntu -g ubuntu "$mountpoint/homelab_ci"

# Swap cushion on the instance-store NVMe. During a synchronized qemu converge
# many guests hit peak RSS at once and can momentarily overshoot the 64 GiB host
# RAM; without swap that overshoot is an OOM-kill that culls a guest and flakes
# its cell. A modest swapfile on the fast, near-idle local NVMe absorbs the
# transient by paging out cold pages instead. vm.swappiness=1 keeps it dormant --
# the kernel reclaims page cache first and only dips into swap as a near-last
# resort, so steady-state cells never pay paging latency. This is a cushion, NOT
# working memory: if it ever fills, the fix is fewer cells per host, not more
# swap. Only on a real NVMe mount (mountpoint true) so the EBS-fallback build
# host -- no instance store -- keeps its small root untouched. Best-effort: a
# swap failure must not fail this unit (the host still runs, just without the
# cushion), so the setup is guarded and swappiness only flips on success.
swap_gib=16
swapfile="$mountpoint/swapfile"
if mountpoint -q "$mountpoint" &&
  ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$swapfile"; then
  # fallocate, not a dd zero-fill: swapon accepts the preallocated file on this
  # ext4/noble host, and it is instant. A multi-GiB zero-fill would instead hold
  # the oneshot in activating for ~40s while it floods page cache -- and since
  # the readiness dirs above are already staged, that delay would needlessly keep
  # the unit (and any later swap-dependent ordering) busy in the boot path.
  if rm -f "$swapfile" &&
    fallocate -l "${swap_gib}G" "$swapfile" &&
    chmod 0600 "$swapfile" &&
    mkswap "$swapfile" >/dev/null &&
    swapon "$swapfile"; then
    sysctl -q -w vm.swappiness=1
  else
    echo "homelab_ci_prepare_scratch: swap setup failed, continuing without cushion" >&2
    swapoff "$swapfile" 2>/dev/null || true
    rm -f "$swapfile" || true
  fi
fi

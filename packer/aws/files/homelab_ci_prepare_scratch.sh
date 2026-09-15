#!/usr/bin/env bash
# Prepare the CI qemu host scratch area on ephemeral block storage.
#
# c8id and friends expose one or more ephemeral NVMe disks (model
# "Amazon EC2 NVMe Instance Storage"). RAID0 them into a single fast volume and
# mount it at /mnt/scratch so every heavy writer lives off the small EBS root:
# the qcow2 overlays under homelab_ci/ and the gitlab-runner checkout, cache,
# and build tree under gitlab-runner/. Instance store is physically wiped on
# stop/terminate, which is exactly right for ephemeral CI scratch.
#
# On EBS-only workers, use the sole non-root EBS disk. Falls back to the EBS
# root filesystem when no scratch disk exists so the bake's own boot succeeds.
# More than one non-root EBS disk is ambiguous and fails closed.
set -euo pipefail

mountpoint=/mnt/scratch

calculate_swap_gib() {
  local meminfo_path=${1:-/proc/meminfo}
  local key="" memory_kib="" unit="" swap_gib
  read -r key memory_kib unit < <(awk '$1 == "MemTotal:" { print $1, $2, $3; exit }' "$meminfo_path") || true
  if [ "$key" != "MemTotal:" ] || ! [[ $memory_kib =~ ^[0-9]+$ ]] || [ "$memory_kib" -eq 0 ] || [ "$unit" != kB ]; then
    echo "homelab_ci_prepare_scratch: invalid MemTotal in ${meminfo_path}" >&2
    return 1
  fi

  swap_gib=$((memory_kib / 4 / 1024 / 1024))
  if [ "$swap_gib" -lt 16 ]; then
    swap_gib=16
  elif [ "$swap_gib" -gt 32 ]; then
    swap_gib=32
  fi
  printf '%s\n' "$swap_gib"
}

if [ "${1:-}" = --calculate-swap-gib ]; then
  calculate_swap_gib "${2:-/proc/meminfo}"
  exit
fi

if ! mountpoint -q "$mountpoint"; then
  mapfile -t devs < <(
    lsblk -dn -o NAME,MODEL | awk '/Instance Storage/ { print "/dev/" $1 }'
  )
  if [ "${#devs[@]}" -eq 0 ]; then
    root_source=$(findmnt -n -o SOURCE /)
    root_disk=$(lsblk -srdpno NAME,TYPE "$root_source" | awk '$2 == "disk" { print $1; exit }')
    mapfile -t ebs_devs < <(
      lsblk -dpno NAME,TYPE,MODEL | awk '$2 == "disk" && /Elastic Block Store/ { print $1 }'
    )
    for dev in "${ebs_devs[@]}"; do
      if [ "$dev" != "$root_disk" ]; then
        devs+=("$dev")
      fi
    done
    if [ "${#devs[@]}" -gt 1 ]; then
      echo "homelab_ci_prepare_scratch: multiple non-root EBS disks are ambiguous" >&2
      exit 1
    fi
  fi
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

# Swap cushion on the ephemeral scratch volume. During a synchronized qemu converge
# many guests hit peak RSS at once and can momentarily overshoot the 64 GiB host
# RAM; without swap that overshoot is an OOM-kill that culls a guest and flakes
# its cell. A modest swapfile on the scratch device absorbs the
# transient by paging out cold pages instead. vm.swappiness=1 keeps it dormant --
# the kernel reclaims page cache first and only dips into swap as a near-last
# resort, so steady-state cells never pay paging latency. This is a cushion, NOT
# working memory: if it ever fills, the fix is fewer cells per host, not more
# swap. Only on a separate scratch mount so the bake host, which falls back to
# its small root filesystem, stays untouched. Best-effort: a
# swap failure must not fail this unit (the host still runs, just without the
# cushion), so the setup is guarded and swappiness only flips on success.
swapfile="$mountpoint/swapfile"
if mountpoint -q "$mountpoint" &&
  ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$swapfile"; then
  # fallocate, not a dd zero-fill: swapon accepts the preallocated file on this
  # ext4/noble host, and it is instant. A multi-GiB zero-fill would instead hold
  # the oneshot in activating for ~40s while it floods page cache -- and since
  # the readiness dirs above are already staged, that delay would needlessly keep
  # the unit (and any later swap-dependent ordering) busy in the boot path.
  if swap_gib=$(calculate_swap_gib); then
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
  else
    echo "homelab_ci_prepare_scratch: swap sizing failed, continuing without cushion" >&2
  fi
fi

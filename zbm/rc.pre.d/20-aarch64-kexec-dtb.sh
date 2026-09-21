#!/usr/bin/env bash
set -euo pipefail

# aarch64 only: ship the fdtput and zstd binaries that
# hooks/early-setup.d/40-kexec-uefi-secure-boot.sh uses. x86_64 boots the unified
# image without a DTB, where the hook exits at once, so its image gains no
# binaries.
[ "$(uname -m)" = aarch64 ] || exit 0

# The build root's dracut.conf.d is linked into place after rc.pre.d runs.
printf 'install_items+=" /usr/bin/fdtput /usr/bin/zstd "\n' >/build/dracut.conf.d/kexec_dtb.conf

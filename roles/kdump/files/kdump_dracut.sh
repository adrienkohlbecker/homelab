#!/bin/bash
set -euo pipefail

# Dracut capture-image generation from Ubuntu kdump-tools 1:1.10.7ubuntu5
# (LP #2042955), for Resolute's package-owned kernel hook and kdump-config.
# Noble retains its package hook and mkinitramfs capture path.
[ -x /usr/sbin/kdump-config ] || exit 0
version="${1:?A kernel version is required}"
linux-version list | grep -Fx "$version" >/dev/null || exit 0
[ "${INITRD-}" != No ] || exit 0
if ischroot; then
  echo "kdump-tools: Skipping capture image generation in a chroot" >&2
  exit 0
fi
if [ -n "${DEB_MAINT_PARAMS-}" ]; then
  eval "set -- $DEB_MAINT_PARAMS"
  [ "${1-}" = configure ] || exit 0
fi

kdumpdir=/var/lib/kdump
target="$kdumpdir/initrd.img-$version"
mkdir -p "$kdumpdir"
trap 'rm -f "$target.new"' EXIT
echo "kdump-tools: Generating $target (dracut)"
# dracut tries --add-confdir as a path before its configuration directories,
# and apt runs this hook from the caller's working directory.
cd /
dracut --force --add-confdir kdump-tools "$target.new" "$version"

# The package's crashkernel estimator consumes the decompressed size in MiB.
if measure_bytes="$(3cpio --examine --raw "$target.new" | awk -F '\t' '{ total += $5 } END { print total + 0 }')" &&
  [ "$measure_bytes" -gt 0 ]; then
  echo $(((measure_bytes + 1024 * 1024 - 1) / (1024 * 1024))) >"$kdumpdir/size_initrd.img-$version"
  sync "$kdumpdir/size_initrd.img-$version"
else
  # The estimator tolerates a missing size; keep the valid capture image.
  rm -f "$kdumpdir/size_initrd.img-$version"
  echo "W: kdump-tools: Cannot determine the capture image size; the crashkernel estimator may be unavailable" >&2
fi
mv "$target.new" "$target"

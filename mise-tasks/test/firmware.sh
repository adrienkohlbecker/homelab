#!/usr/bin/env bash
#MISE description="Fetch the pinned newer edk2 aarch64 firmware (warm-reboot fix) into test/firmware/"
set -euo pipefail

# Homebrew's qemu (through 11.0.1) bundles edk2-stable202408, whose DXE pool
# allocator hits a heap ASSERT (MdeModulePkg/Core/Dxe/Mem/Pool.c) when rEFInd
# boots the OS across an aarch64 *warm* reboot -- so any test that reboots
# (hwe_kernel seed, reboot/kdump/console _verify) wedges the firmware and times
# out. edk2-stable202511 fixes it. There is no Homebrew formula for a newer
# build and qemu has not bumped its bundled blob, so source it from Debian's
# edk2 package (qemu-efi-aarch64) and drop it where test/arch.py looks first.
#
# Pinned by hash to snapshot.debian.org so the URL never rots when sid bumps
# the version (the live pool URL would be
# http://deb.debian.org/debian/pool/main/e/edk2/qemu-efi-aarch64_2025.11-5_all.deb).
# Bump all three together: query
#   https://snapshot.debian.org/mr/binary/qemu-efi-aarch64/<ver>/binfiles
# for the new sha1, download to re-derive the sha256s, and update arch.py's
# firmware filename to match.
DEB_VERSION="2025.11-5" # edk2-stable202511
DEB_URL="https://snapshot.debian.org/file/137d1a34bd9ec2e10b1d81331e92d163824579c4"
DEB_SHA256="95388b7606e821dd8af1dd852767094d569ff78cb2e8f1dc218b60959a52ee81"
FW_SHA256="4003fc28e677193432558b717b74221fae671c92aeef174b84ecc2c3242fd013"

root="$(git rev-parse --show-toplevel)"
dest="${root}/test/firmware/edk2-aarch64-code-202511.fd"

# shasum is the macOS builtin (the aarch64 fixture is the local Mac); fall back
# to sha256sum on Linux so a Linux-aarch64 dev can run this too.
sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

if [ -f "${dest}" ] && [ "$(sha256 "${dest}")" = "${FW_SHA256}" ]; then
  echo "==> edk2 ${DEB_VERSION} firmware already present and verified at ${dest}"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "==> Fetching edk2 ${DEB_VERSION} firmware (${DEB_URL})"
curl -fsSL -o "${tmp}/edk2.deb" "${DEB_URL}"
got="$(sha256 "${tmp}/edk2.deb")"
if [ "${got}" != "${DEB_SHA256}" ]; then
  echo "ERROR: .deb sha256 mismatch: expected ${DEB_SHA256}, got ${got}" >&2
  exit 1
fi

# A .deb is an ar archive; the firmware lives in its data tarball. BSD ar
# (macOS) and GNU ar both extract it; tar auto-detects the xz compression.
(cd "${tmp}" && ar x edk2.deb)
tar -xf "${tmp}"/data.tar.* -C "${tmp}" ./usr/share/AAVMF/AAVMF_CODE.no-secboot.fd
fw="${tmp}/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd"
got="$(sha256 "${fw}")"
if [ "${got}" != "${FW_SHA256}" ]; then
  echo "ERROR: firmware sha256 mismatch: expected ${FW_SHA256}, got ${got}" >&2
  exit 1
fi

mkdir -p "$(dirname "${dest}")"
mv "${fw}" "${dest}"
echo "==> Installed edk2 ${DEB_VERSION} firmware at ${dest}"

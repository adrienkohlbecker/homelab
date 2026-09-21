#!/usr/bin/env bash
#MISE description="Fetch the pinned newer edk2 aarch64 firmware (warm-reboot fix) into test/firmware/"
set -euo pipefail

# Homebrew's qemu (through 11.0.1) and Ubuntu Noble bundle edk2-stable202408,
# whose DXE pool allocator hits a heap ASSERT (MdeModulePkg/Core/Dxe/Mem/Pool.c)
# when rEFInd boots the OS across an aarch64 *warm* reboot. Any test that
# reboots (reboot/kdump/console _verify) then wedges the firmware and times out.
# edk2-stable202511 fixes it. Source the matching CODE and VARS templates
# from Debian's content-addressed qemu-efi-aarch64 package; their immutable URL
# and checksums live with the other upstream pins in versions.yml.
#
# This is the only extractor: the ARM qemu-host AMI runs this script from a
# minimal copy of the repo layout, so it resolves its inputs relative to itself
# and needs only python3 with PyYAML, curl, ar, and tar. The output file names
# are fixed here and mirrored by test/arch.py and packer/qemu.pkr.hcl;
# unit_tests/test_packer_tasks.py checks they agree.

# Only aarch64 guests boot the pinned pair, and fixtures always boot natively,
# so other hosts (the x86 lab builder) have nothing to fetch.
case "$(uname -m)" in
arm64 | aarch64) ;;
*)
  echo "==> $(uname -m) host boots packaged firmware; nothing to fetch"
  exit 0
  ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
firmware_dir="${HOMELAB_AARCH64_FIRMWARE_DIR:-${root}/test/firmware}"
archive_marker="${firmware_dir}/archive.sha256"
# The pinned package pairs the plain (no Secure Boot) CODE image with VARS.
code_dest="${firmware_dir}/edk2-aarch64-code.fd"
vars_dest="${firmware_dir}/edk2-aarch64-vars.fd"

{
  read -r deb_version
  read -r deb_url
  read -r deb_sha256
} < <(
  python3 - "${root}/group_vars/all/versions.yml" <<'PY'
import sys

import yaml

with open(sys.argv[1]) as versions_file:
    versions = yaml.safe_load(versions_file)
artifact = versions["qemu_efi_aarch64_artifact"]
print(
    versions["qemu_efi_aarch64_version"],
    artifact["url"],
    artifact["sha256"],
    sep="\n",
)
PY
)

# shasum is the macOS builtin (the aarch64 fixture is the local Mac); Linux
# hosts may only have coreutils' sha256sum. Both check GNU checksum lines.
verify_sha256() {
  local expected=$1 file=$2
  local -a checker=(sha256sum)
  if command -v shasum >/dev/null 2>&1; then
    checker=(shasum -a 256)
  fi
  if ! printf '%s  %s\n' "${expected}" "${file}" | "${checker[@]}" -c - >/dev/null; then
    echo "ERROR: $(basename "${file}") sha256 mismatch: expected ${expected}" >&2
    exit 1
  fi
}

if [ -f "${code_dest}" ] && [ -f "${vars_dest}" ] && [ "$(awk 'NR == 1 { print; exit }' "${archive_marker}" 2>/dev/null || true)" = "${deb_sha256}" ]; then
  echo "==> edk2 ${deb_version} firmware already present at ${firmware_dir}"
  exit 0
fi

# A root-owned AMI path is populated only when the qemu-host AMI is baked, after
# verifying the pinned archive. Older promoted AMIs predate archive.sha256;
# reuse their immutable pair instead of trying to overwrite it.
if [ -f "${code_dest}" ] && [ -f "${vars_dest}" ] && [ ! -w "${firmware_dir}" ]; then
  echo "==> Using preinstalled edk2 ${deb_version} firmware at ${firmware_dir}"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "==> Fetching edk2 ${deb_version} firmware (${deb_url})"
# snapshot.debian.org throttles bursts; retry transient failures.
curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${tmp}/edk2.deb" "${deb_url}"
verify_sha256 "${deb_sha256}" "${tmp}/edk2.deb"

# A .deb is an ar archive; the firmware lives in its data tarball. BSD ar
# (macOS) and GNU ar both extract it; tar auto-detects the xz compression.
(cd "${tmp}" && ar x edk2.deb)
tar -xf "${tmp}"/data.tar.* -C "${tmp}" \
  ./usr/share/AAVMF/AAVMF_CODE.no-secboot.fd \
  ./usr/share/AAVMF/AAVMF_VARS.fd

mkdir -p "${firmware_dir}"
install -m 0644 "${tmp}/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd" "${code_dest}"
install -m 0644 "${tmp}/usr/share/AAVMF/AAVMF_VARS.fd" "${vars_dest}"
printf '%s\n' "${deb_sha256}" >"${archive_marker}.tmp"
mv "${archive_marker}.tmp" "${archive_marker}"
echo "==> Installed edk2 ${deb_version} CODE and VARS firmware at ${firmware_dir}"

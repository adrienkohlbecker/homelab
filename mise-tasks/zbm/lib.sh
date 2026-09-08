#!/usr/bin/env bash

zbm_host_arch() {
  uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/
}

zbm_upstream_arch() {
  uname -m | sed -e s/amd64/x86_64/
}

zbm_repo_root() {
  local repo_root="${MISE_CONFIG_ROOT:-}"

  if [ -z "$repo_root" ] || [ ! -d "${repo_root}/zbm" ]; then
    repo_root="$(git rev-parse --show-toplevel)"
  fi
  (cd "$repo_root" && pwd -P)
}

zbm_latest_tarball() {
  local out_dir=$1 arch=$2 tarballs

  # ls -t keeps the newest artifact by mtime; keep it behind one helper so
  # callers can handle the no-match case without pipefail swallowing the error.
  # shellcheck disable=SC2012
  tarballs=$(ls -t "${out_dir}"/zfsbootmenu-v*-"${arch}".tar.gz 2>/dev/null) || return 1
  printf '%s\n' "${tarballs%%$'\n'*}"
}

zbm_lsinitrd() {
  local builder_tag=$1 image=$2 mount_root

  mount_root="$(dirname "$image")"
  docker run --rm \
    --entrypoint /usr/bin/lsinitrd \
    -v "${mount_root}:/work:ro" \
    "$builder_tag" \
    "/work/$(basename "$image")"
}

zbm_assert_core_listing() {
  local listing=$1 label=$2 required

  for required in \
    "usr/bin/reboot" \
    "usr/bin/poweroff -> reboot" \
    "usr/bin/shutdown -> reboot" \
    "usr/bin/firmware-setup -> reboot"; do
    if ! grep -qF "$required" "$listing"; then
      echo "Power command paths in ${label}:" >&2
      awk 'NF >= 9 && $9 ~ /(^|\/)(reboot|poweroff|shutdown|firmware-setup)$/ { print "  " $9, $10, $11 }' "$listing" >&2
      echo "${label}: missing required ZFSBootMenu recovery command /${required%% *}" >&2
      return 1
    fi
  done

  for required in zfs spl; do
    if ! grep -Eq "/${required}[.]ko([.]|$)" "$listing"; then
      echo "${label}: missing required ZFS kernel module ${required}.ko" >&2
      return 1
    fi
  done
}

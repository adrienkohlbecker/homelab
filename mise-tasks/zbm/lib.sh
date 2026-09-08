#!/usr/bin/env bash

zbm_host_arch() {
  uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/
}

zbm_repo_root() {
  local repo_root="${MISE_CONFIG_ROOT:-}"

  if [ -z "$repo_root" ] || [ ! -d "${repo_root}/zbm" ]; then
    repo_root="$(git rev-parse --show-toplevel)"
  fi
  (cd "$repo_root" && pwd -P)
}

zbm_local_tarball() {
  local out_dir=$1 arch=$2
  local -a tarballs

  mapfile -t tarballs < <(compgen -G "${out_dir}/zfsbootmenu-v*-${arch}.tar.gz")
  if [ "${#tarballs[@]}" -ne 1 ]; then
    echo "expected exactly one ${arch} tarball in ${out_dir}, found ${#tarballs[@]} — run 'mise run zbm:build' to refresh it" >&2
    return 1
  fi
  printf '%s\n' "${tarballs[0]}"
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

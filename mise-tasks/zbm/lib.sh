#!/usr/bin/env bash

zbm_host_arch() {
  uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/
}

zbm_upstream_arch() {
  uname -m | sed -e s/amd64/x86_64/
}

zbm_repo_root() {
  local repo_root="${MISE_CONFIG_ROOT:-}"

  if [ -z "$repo_root" ] || { [ "${repo_root#/}" = "$repo_root" ] && [ ! -d "${repo_root}/zbm" ]; }; then
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

zbm_install_make_binary_wrappers() {
  local wrapper_dir=$1 docker_bin

  mkdir -p "$wrapper_dir"
  if command -v grealpath >/dev/null 2>&1; then
    ln -sf "$(command -v grealpath)" "${wrapper_dir}/realpath"
  else
    cat >"${wrapper_dir}/realpath" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

paths=()
while [ "$#" -gt 0 ]; do
  case "$1" in
  -e | -q)
    shift
    ;;
  --)
    shift
    paths+=("$@")
    break
    ;;
  -*)
    echo "realpath wrapper: unsupported option $1" >&2
    exit 1
    ;;
  *)
    paths+=("$1")
    shift
    ;;
  esac
done

for path in "${paths[@]}"; do
  [ -e "$path" ] || exit 1
done
python3 -c 'import os, sys; [print(os.path.realpath(path)) for path in sys.argv[1:]]' "${paths[@]}"
WRAPPER
    chmod +x "${wrapper_dir}/realpath"
  fi

  # Upstream make-binary.sh invokes podman; this repo builds through docker.
  docker_bin="$(command -v docker)"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -euo pipefail\n'
    printf 'docker_bin=%q\n' "$docker_bin"
    cat <<'WRAPPER'
args=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-v" ] && [ "$#" -ge 2 ]; then
    volume="$2"
    case "$volume" in
    .:*) volume="${PWD}${volume#.}" ;;
    esac
    args+=("-v" "$volume")
    shift 2
    continue
  fi
  args+=("$1")
  shift
done
exec "$docker_bin" "${args[@]}"
WRAPPER
  } >"${wrapper_dir}/podman"
  chmod +x "${wrapper_dir}/podman"
}

zbm_lsinitrd() {
  local builder_tag=$1 image=$2 mount_root mount_image

  mount_root="$(dirname "$image")"
  mount_image="${image#"${mount_root}/"}"
  docker run --rm \
    --entrypoint /usr/bin/lsinitrd \
    -v "${mount_root}:/work:ro" \
    "$builder_tag" \
    "/work/${mount_image}"
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

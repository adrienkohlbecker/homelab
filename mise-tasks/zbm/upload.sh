#!/usr/bin/env bash
#MISE description="Upload the built ZFSBootMenu tarball + checksum to Gitea generic packages"
# Generates a .sha256sum sidecar next to the .tar.gz, then PUTs both to
# gitea.lab.fahm.fr's generic package repo at
# adrienkohlbecker/zfsbootmenu/<zbm_version>/<file>.tar.gz. One stable
# filename per (version, arch); Gitea rejects PUT to an existing path,
# so we DELETE first (404 on the first upload is fine). chroot.sh
# references the same stable filename, so a rebuild propagates without
# source edits — bump zbm_version (mise.toml [vars]) only when moving
# to a new upstream release. Token is read from 1Password at runtime.
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
local_name="zfsbootmenu-v${ZBM_VERSION}-${arch}.tar.gz"
sum_name="${local_name}.sha256sum"

(cd "$out_dir" && sha256sum "$local_name") >"${out_dir}/${sum_name}"

username="$(op read 'op://Lab/Gitea package upload token/username')"
token="$(op read 'op://Lab/Gitea package upload token/password')"
base_url="https://gitea.lab.fahm.fr/api/packages/adrienkohlbecker/generic/zfsbootmenu/${ZBM_VERSION}"

for name in "${local_name}" "${sum_name}"; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' --user "${username}:${token}" -X DELETE "${base_url}/${name}")"
  case "${status}" in
  204 | 404) ;;
  *)
    echo "DELETE ${base_url}/${name} returned ${status}" >&2
    exit 1
    ;;
  esac
done

curl --fail-with-body --user "${username}:${token}" --upload-file "${out_dir}/${local_name}" "${base_url}/${local_name}"
echo "${local_name}: uploaded to ${base_url}/${local_name}"
curl --fail-with-body --user "${username}:${token}" --upload-file "${out_dir}/${sum_name}" "${base_url}/${sum_name}"
echo "${sum_name}: uploaded to ${base_url}/${sum_name}"

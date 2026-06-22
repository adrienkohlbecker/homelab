#!/usr/bin/env bash
#MISE description="Publish the built ZFSBootMenu tarball + checksum to the GitLab generic package registry"
# PUTs both the .tar.gz and its .sha256sum sidecar (produced by build.sh) to
# the akohlbecker/homelab project's generic package registry (project
# 83079143; numeric id keeps the path free of an encoded slash) under
# package zfsbootmenu/<version>/. The registry accepts re-publishes of new
# version strings unconditionally; no DELETE required. Downloads are
# anonymous (public project) — only this publish path needs auth.
#
# Auth, by environment:
#   GitLab CI:   CI_JOB_TOKEN (the zbm_build job) — built-in write access to
#                this project's package registry, no standing secret. curl,
#                not glab: glab only authenticates via PRIVATE-TOKEN/Bearer
#                (a PAT shape job tokens are not valid as), while the
#                packages API honors job tokens solely through the JOB-TOKEN
#                header.
#   Workstation: the operator's authenticated glab CLI (`glab auth login`),
#                covering the locally-built aarch64 tarballs CI does not
#                produce. No deploy token to mint or store.
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
repo_root="$(zbm_repo_root)"
out_dir="${repo_root}/zbm-build/${arch}"
if ! tarball="$(zbm_latest_tarball "$out_dir" "$arch")"; then
  echo "no tarball in ${out_dir} — run 'mise run zbm:build' first" >&2
  exit 1
fi
tarball="$(basename "$tarball")"
sha256sum_file="${tarball}.sha256sum"

for f in "$tarball" "$sha256sum_file"; do
  test -f "${out_dir}/${f}" || {
    echo "missing ${out_dir}/${f} — run 'mise run zbm:build' first" >&2
    exit 1
  }
done

# Package version = the full version string embedded in the filename
# (zfsbootmenu-<version>.tar.gz), arch suffix included — matching the
# consumers' <base>/<version>/zfsbootmenu-<version>.tar.gz layout
# (roles/zfsbootmenu/vars/main.yml, packer/scripts/chroot.sh).
version="${tarball#zfsbootmenu-}"
version="${version%.tar.gz}"

project_id="${CI_PROJECT_ID:-83079143}"
api_path="projects/${project_id}/packages/generic/zfsbootmenu/${version}"

if [[ -n "${CI_JOB_TOKEN:-}" ]]; then
  # Keep the token out of argv (visible in ps) by feeding curl a config file.
  header_file="$(mktemp)"
  chmod 600 "$header_file"
  trap 'rm -f "$header_file"' EXIT
  printf 'header = "JOB-TOKEN: %s"\n' "$CI_JOB_TOKEN" >"$header_file"
else
  # The repo's origin is GitHub, so pin glab to the gitlab.com identity
  # rather than letting it guess a host from the remote.
  export GITLAB_HOST=gitlab.com
  glab auth status >/dev/null 2>&1 || {
    echo "glab is not authenticated against gitlab.com — run 'glab auth login'" >&2
    exit 1
  }
fi

for name in "$tarball" "$sha256sum_file"; do
  if [[ -n "${CI_JOB_TOKEN:-}" ]]; then
    curl --fail-with-body -sS --retry 3 --retry-delay 5 --retry-connrefused \
      --config "$header_file" \
      --upload-file "${out_dir}/${name}" "${CI_API_V4_URL}/${api_path}/${name}"
    echo
  else
    glab api "${api_path}/${name}" -X PUT --input "${out_dir}/${name}" >/dev/null
  fi
  echo "published ${name}"
done

#!/usr/bin/env bash
#MISE description="Publish the built ZFSBootMenu tarball + checksum to the GitLab generic package registry"
# PUTs both the .tar.gz and its .sha256sum sidecar (produced by build.sh) to
# the akohlbecker/homelab project's generic package registry (project
# 83079143; numeric id keeps the path free of an encoded slash) under
# package zfsbootmenu/<version>/. The registry accepts re-publishes of new
# version strings unconditionally; no DELETE required. Downloads are
# anonymous (public project) — only this publish path needs auth.
#
# Credential: GITLAB_ZBM_DEPLOY_TOKEN, a project deploy token scoped to
# write_package_registry.
#   CI:          injected by the zbm-build workflow step (pushed to the repo
#                secret by terraform/github.tf).
#   Workstation: op:// reference from mise.toml [env]; re-exec under
#                op run -- to resolve it.
set -euo pipefail

# Re-exec under op run -- only on the workstation (CI is not set) when the
# token is still an unresolved op:// reference.
if [[ -z "${CI:-}" ]] &&
  [[ "${GITLAB_ZBM_DEPLOY_TOKEN:-}" == op://* ]] &&
  [[ -z "${_ZBM_UPLOAD_OP_RESOLVED:-}" ]]; then
  export _ZBM_UPLOAD_OP_RESOLVED=1
  exec op run -- "$0" "$@"
fi

# Skip gracefully when the token is unavailable or still an unresolved op://
# ref (e.g., CI before the deploy token secret is provisioned by terraform
# apply).
if [[ -z "${GITLAB_ZBM_DEPLOY_TOKEN:-}" ]] ||
  [[ "${GITLAB_ZBM_DEPLOY_TOKEN:-}" == op://* ]]; then
  echo "GITLAB_ZBM_DEPLOY_TOKEN not available; skipping upload" \
    "(run 'mise run tf apply' to provision the deploy token secret)" >&2
  exit 0
fi

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
# shellcheck disable=SC2012
tarball="$(ls -t "${out_dir}"/zfsbootmenu-v*.tar.gz 2>/dev/null | head -1)"
[ -n "$tarball" ] || {
  echo "no tarball in ${out_dir} — run 'mise run zbm:build' first" >&2
  exit 1
}
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

dest_base="https://gitlab.com/api/v4/projects/83079143/packages/generic/zfsbootmenu/${version}"
# Keep the token out of argv (visible in ps) by feeding curl a config file.
header_file="$(mktemp)"
chmod 600 "$header_file"
trap 'rm -f "$header_file"' EXIT
printf 'header = "DEPLOY-TOKEN: %s"\n' "$GITLAB_ZBM_DEPLOY_TOKEN" >"$header_file"
unset GITLAB_ZBM_DEPLOY_TOKEN

for name in "$tarball" "$sha256sum_file"; do
  curl --fail-with-body -sS --retry 3 --retry-delay 5 --retry-connrefused \
    --config "$header_file" \
    --upload-file "${out_dir}/${name}" "${dest_base}/${name}"
  echo
  echo "published ${name}"
done

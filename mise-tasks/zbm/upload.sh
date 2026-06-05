#!/usr/bin/env bash
#MISE description="Upload the built ZFSBootMenu tarball + checksum to the Nexus zbm raw repo"
# PUTs both the .tar.gz and its .sha256sum sidecar (produced by build.sh)
# to nexus.lab.fahm.fr/repository/zbm. Nexus raw repos accept new filenames
# unconditionally (write_policy=ALLOW); no DELETE required.
# NEXUS_USERNAME/PASSWORD come from mise's [env] (op:// references).
set -euo pipefail

# Re-exec under op run -- if NEXUS_USERNAME is still an op:// reference
# (file-based mise tasks don't get the [env] block resolved automatically).
if [[ "${NEXUS_USERNAME:-}" == op://* ]] && [[ -z "${_ZBM_UPLOAD_OP_RESOLVED:-}" ]]; then
  export _ZBM_UPLOAD_OP_RESOLVED=1
  exec op run -- "$0" "$@"
fi

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
tarball="zfsbootmenu-v${ZBM_VERSION}-${arch}.tar.gz"
sha256sum="${tarball}.sha256sum"

for f in "$tarball" "$sha256sum"; do
  test -f "${out_dir}/${f}" || { echo "missing ${out_dir}/${f} — run 'mise run zbm:build' first" >&2; exit 1; }
done

dest_base="https://nexus.lab.fahm.fr/repository/zbm"
netrc="$(mktemp)"
chmod 600 "$netrc"
trap 'rm -f "$netrc"' EXIT
printf 'machine nexus.lab.fahm.fr login %s password %s\n' \
  "$NEXUS_USERNAME" "$NEXUS_PASSWORD" >"$netrc"
unset NEXUS_PASSWORD

for name in "$tarball" "$sha256sum"; do
  curl --fail-with-body -sS --retry 3 --retry-delay 5 --retry-connrefused \
    --netrc-file "$netrc" \
    --upload-file "${out_dir}/${name}" "${dest_base}/${name}"
  echo "uploaded ${name}"
done

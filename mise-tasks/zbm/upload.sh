#!/usr/bin/env bash
#MISE description="Upload the built ZFSBootMenu tarball + checksum to the Nexus zbm raw repo"
# PUTs both the .tar.gz and its .sha256sum sidecar (produced by build.sh)
# to nexus.lab.fahm.fr/repository/zbm. Nexus raw repos accept new filenames
# unconditionally (write_policy=ALLOW); no DELETE required.
#
# Credential sources (first wins):
#   CI:          NEXUS_ZBM_USERNAME / NEXUS_ZBM_PASSWORD injected by the workflow
#                step (terraform-managed dedicated secrets, NOT in mise.toml [env]
#                so mise doesn't touch them).
#   Workstation: NEXUS_USERNAME / NEXUS_PASSWORD from mise.toml [env] as op://
#                references; re-exec under op run -- to resolve them.
set -euo pipefail

# Re-exec under op run -- only on the workstation (CI is not set) when
# NEXUS_ZBM_USERNAME is absent and NEXUS_USERNAME is still an op:// reference.
if [[ -z "${NEXUS_ZBM_USERNAME:-}" ]] && \
   [[ -z "${CI:-}" ]] && \
   [[ "${NEXUS_USERNAME:-}" == op://* ]] && \
   [[ -z "${_ZBM_UPLOAD_OP_RESOLVED:-}" ]]; then
  export _ZBM_UPLOAD_OP_RESOLVED=1
  exec op run -- "$0" "$@"
fi

nexus_user="${NEXUS_ZBM_USERNAME:-${NEXUS_USERNAME:-}}"
nexus_pass="${NEXUS_ZBM_PASSWORD:-${NEXUS_PASSWORD:-}}"
# Skip gracefully when credentials are unavailable or still unresolved op:// refs
# (e.g., CI before the zbm repo + secrets are provisioned by terraform apply).
if [[ -z "${nexus_user:-}" ]] || [[ -z "${nexus_pass:-}" ]] || \
   [[ "${nexus_user:-}" == op://* ]] || [[ "${nexus_pass:-}" == op://* ]]; then
  echo "Nexus credentials not available; skipping upload" \
       "(run 'mise run tf apply' to provision the zbm repo + creds)" >&2
  exit 0
fi

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
tarball="$(ls -t "${out_dir}"/zfsbootmenu-v*.tar.gz 2>/dev/null | head -1)"
[ -n "$tarball" ] || { echo "no tarball in ${out_dir} — run 'mise run zbm:build' first" >&2; exit 1; }
tarball="$(basename "$tarball")"
sha256sum_file="${tarball}.sha256sum"

for f in "$tarball" "$sha256sum_file"; do
  test -f "${out_dir}/${f}" || { echo "missing ${out_dir}/${f} — run 'mise run zbm:build' first" >&2; exit 1; }
done

dest_base="https://nexus.lab.fahm.fr/repository/zbm"
netrc="$(mktemp)"
chmod 600 "$netrc"
trap 'rm -f "$netrc"' EXIT
printf 'machine nexus.lab.fahm.fr login %s password %s\n' \
  "$nexus_user" "$nexus_pass" >"$netrc"
unset nexus_pass

for name in "$tarball" "$sha256sum_file"; do
  curl --fail-with-body -sS --retry 3 --retry-delay 5 --retry-connrefused \
    --netrc-file "$netrc" \
    --upload-file "${out_dir}/${name}" "${dest_base}/${name}"
  echo "uploaded ${name}"
done

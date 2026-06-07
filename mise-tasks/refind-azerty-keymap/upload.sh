#!/usr/bin/env bash
#MISE description="Upload rEFInd AZERTY keymap EFI artifacts to the Nexus zbm raw repo"
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="${MISE_CONFIG_ROOT:-$(git -C "${script_dir}/../.." rev-parse --show-toplevel)}"

# Re-exec under op run -- only on the workstation when the generic Nexus
# credentials are still unresolved op:// references. CI injects NEXUS_ZBM_*.
if [[ -z "${NEXUS_ZBM_USERNAME:-}" ]] &&
  [[ -z "${CI:-}" ]] &&
  [[ "${NEXUS_USERNAME:-}" == op://* ]] &&
  [[ -z "${_REFIND_AZERTY_KEYMAP_UPLOAD_OP_RESOLVED:-}" ]]; then
  export _REFIND_AZERTY_KEYMAP_UPLOAD_OP_RESOLVED=1
  exec op run -- "$0" "$@"
fi

nexus_user="${NEXUS_ZBM_USERNAME:-${NEXUS_USERNAME:-}}"
nexus_pass="${NEXUS_ZBM_PASSWORD:-${NEXUS_PASSWORD:-}}"
if [[ -z "${nexus_user:-}" ]] || [[ -z "${nexus_pass:-}" ]] ||
  [[ "${nexus_user:-}" == op://* ]] || [[ "${nexus_pass:-}" == op://* ]]; then
  echo "Nexus credentials not available; skipping upload" \
    "(NEXUS_ZBM_USERNAME/NEXUS_ZBM_PASSWORD expected in CI)" >&2
  exit 0
fi

artifact_root="${REFIND_AZERTY_KEYMAP_BUILD_DIR:-${repo_root}/efi/refind-azerty-keymap/build}"
version="${REFIND_AZERTY_KEYMAP_VERSION:-}"
if [[ -z "$version" && -f "${repo_root}/efi/refind-azerty-keymap/VERSION" ]]; then
  version="$(<"${repo_root}/efi/refind-azerty-keymap/VERSION")"
fi
prefix="${REFIND_AZERTY_KEYMAP_UPLOAD_PREFIX:-refind-azerty-keymap/${version:-manual}}"
dest_base="https://nexus.lab.fahm.fr/repository/zbm/${prefix}"

if [[ ! -d "$artifact_root" ]]; then
  echo "artifact root not found: ${artifact_root}" >&2
  exit 1
fi

files=()
while IFS= read -r file; do
  files+=("$file")
done < <(
  find "$artifact_root" -mindepth 2 -type f \
    \( -name '*.EFI' -o -name '*.efi' -o -name '*.sha256sum' \) \
    -print | sort
)

if [[ "${#files[@]}" -eq 0 ]]; then
  echo "no EFI artifacts found under ${artifact_root}" >&2
  exit 1
fi

netrc="$(mktemp)"
chmod 600 "$netrc"
trap 'rm -f "$netrc"' EXIT
printf 'machine nexus.lab.fahm.fr login %s password %s\n' \
  "$nexus_user" "$nexus_pass" >"$netrc"
unset nexus_pass

for file in "${files[@]}"; do
  rel="${file#"${artifact_root}"/}"
  curl --fail-with-body -sS --retry 3 --retry-delay 5 --retry-connrefused \
    --netrc-file "$netrc" \
    --upload-file "$file" "${dest_base}/${rel}"
  echo "uploaded ${prefix}/${rel}"
done

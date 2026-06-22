#!/usr/bin/env bash
#MISE description="Compare the local ZFSBootMenu recovery initramfs with upstream"
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

version="${ZBM_VERSION:-3.0.1}"
kernel="${ZBM_KERNEL_VERSION:-6.1}"
official_arch=x86_64

for required in curl docker git sha256sum tar; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "missing required command: ${required}" >&2
    exit 1
  fi
done

repo_root="$(zbm_repo_root)"
local_arch="$(zbm_host_arch)"

src_dir="${repo_root}/zbm-build/src"
official_asset_base="zfsbootmenu-recovery-${official_arch}-v${version}-linux${kernel}"
asset_url_base="https://github.com/zbm-dev/zfsbootmenu/releases/download/v${version}"
builder_tag="localhost/zbm-builder:v${version}-${local_arch}"
report_dir="${repo_root}/zbm-build/upstream-compare/reports/local-recovery-${local_arch}-vs-official-${official_arch}-v${version}-linux${kernel}"

if ! local_tar="$(zbm_latest_tarball "${repo_root}/zbm-build/${local_arch}" "$local_arch")"; then
  echo "no ${local_arch} ZBM tarball found; run 'mise run zbm:build' first" >&2
  exit 1
fi
if [ ! -s "$local_tar" ]; then
  echo "local ZBM tarball is missing or empty: ${local_tar}" >&2
  exit 1
fi

overlay_source_roots=(
  "${repo_root}/mise-tasks/zbm/build.sh"
  "${repo_root}/mise-tasks/zbm/lib.sh"
  "${repo_root}/zbm/recovery-overlay.patch"
  "${repo_root}/zbm/dracut.conf.d/recovery.conf"
  "${repo_root}/zbm/dracut.conf.d/user_hooks.conf"
  "${repo_root}/zbm/dropbear"
  "${repo_root}/zbm/hooks"
)
while IFS= read -r source; do
  if [ "$source" -nt "$local_tar" ]; then
    echo "local ZBM tarball is older than ${source}" >&2
    echo "run 'mise run zbm:build' before comparing current overlay output" >&2
    exit 1
  fi
done < <(find "${overlay_source_roots[@]}" -type f -print)

if ! docker image inspect "$builder_tag" >/dev/null 2>&1; then
  echo "comparison builder image not found: ${builder_tag}" >&2
  echo "run 'mise run zbm:builder-image' first" >&2
  exit 1
fi
if [ "$local_arch" != "$official_arch" ] && [ ! -d "$src_dir/.git" ]; then
  echo "ZBM source not found at $src_dir; run 'mise run zbm:builder-image' first" >&2
  exit 1
fi

workdir="$(mktemp -d)"

remove_path() {
  chmod -R u+rwX "$@" 2>/dev/null || true
  rm -rf "$@"
}

preserve_on_error() {
  local status=$?
  trap - EXIT
  echo "Preserving failed comparison workdir: ${workdir}" >&2
  exit "$status"
}

trap 'remove_path "$workdir"' EXIT
trap preserve_on_error ERR INT TERM

workdir="$(realpath "$workdir")"
wrapper_dir="${workdir}/bin"
zbm_install_make_binary_wrappers "$wrapper_dir"
export PATH="${wrapper_dir}:${PATH}"
official_dir="${workdir}/official"
extract_dir="${workdir}/extract"
for generated_dir in "$official_dir" "$extract_dir" "$report_dir"; do
  if [ -e "$generated_dir" ]; then
    remove_path "$generated_dir"
  fi
done
mkdir -p "$official_dir" "$extract_dir" "$report_dir"

official_tar="${official_dir}/${official_asset_base}.tar.gz"

echo "Working directory: ${workdir}"
echo "Report directory: ${report_dir}"
echo "Local tarball: ${local_tar}"
echo "Local architecture: ${local_arch}"
echo "Official architecture: ${official_arch}"
echo "Comparison builder image: ${builder_tag}"
echo "Downloading official upstream artifacts"
curl -fsSL "${asset_url_base}/${official_asset_base}.tar.gz" -o "$official_tar"
curl -fsSL "${asset_url_base}/sha256.txt" -o "${official_dir}/sha256.txt"

(
  cd "$official_dir"
  awk -v tar="${official_asset_base}.tar.gz" '
    $1 == "SHA256" && $3 == "=" {
      name = $2
      sub(/^\(/, "", name)
      sub(/\)$/, "", name)
      if (name == tar) {
        print $4 "  " name
      }
      next
    }
    $NF == tar || $NF == "./" tar { print }
  ' sha256.txt >sha256.selected
  selected_count="$(wc -l <sha256.selected | tr -d '[:space:]')"
  if [ "$selected_count" -ne 1 ]; then
    echo "expected 1 checksum entry for ${official_asset_base}, found ${selected_count}" >&2
    sed -n '1,40p' sha256.txt >&2
    exit 1
  fi
  sha256sum -c sha256.selected
)

mkdir -p "${extract_dir}/local" "${extract_dir}/official"
tar -xzf "$local_tar" -C "${extract_dir}/local"
tar -xzf "$official_tar" -C "${extract_dir}/official"

find_one() {
  local root="$1"
  local pattern="$2"
  local found=()
  mapfile -t found < <(find "$root" -type f -name "$pattern" | sort)
  if [ "${#found[@]}" -ne 1 ]; then
    echo "expected one ${pattern} under ${root}, found ${#found[@]}" >&2
    printf '%s\n' "${found[@]}" >&2
    return 1
  fi
  printf '%s\n' "${found[0]}"
}

local_initramfs="$(find_one "${extract_dir}/local" 'initramfs*.img')"
official_initramfs="$(find_one "${extract_dir}/official" 'initramfs*.img')"

zbm_lsinitrd "$builder_tag" "$local_initramfs" >"${report_dir}/local.lsinitrd"
zbm_lsinitrd "$builder_tag" "$official_initramfs" >"${report_dir}/official.lsinitrd"

extract_paths() {
  awk 'NF >= 9 && $1 ~ /^[-l]/ {
    path = $9
    target = ""
    if ($10 == "->") {
      target = " -> " $11
    }
    print path target
  }' "$1"
}

extract_modules() {
  extract_paths "$1" |
    awk '/[.]ko([.]|$)/ { print }' |
    sed -E 's#usr/lib/modules/[^/]+/#usr/lib/modules/<kver>/#' |
    sort -u
}

extract_binaries() {
  extract_paths "$1" |
    awk '/^((usr\/)?s?bin|libexec)\// { print }' |
    sort -u
}

extract_modules "${report_dir}/local.lsinitrd" >"${report_dir}/local.modules"
extract_modules "${report_dir}/official.lsinitrd" >"${report_dir}/official.modules"
extract_binaries "${report_dir}/local.lsinitrd" >"${report_dir}/local.binaries"
extract_binaries "${report_dir}/official.lsinitrd" >"${report_dir}/official.binaries"

local_upstream_missing_module_count=0
if [ "$local_arch" != "$official_arch" ]; then
  local_upstream_src="${workdir}/local-upstream-src"
  local_upstream_extract="${extract_dir}/local-upstream"
  local_upstream_asset_base="zfsbootmenu-recovery-$(zbm_upstream_arch)-v${version}-linux${kernel}"
  local_upstream_asset_dir="${local_upstream_src}/releng/assets/${version}"
  local_upstream_tar="${local_upstream_asset_dir}/${local_upstream_asset_base}.tar.gz"

  echo "Building pristine upstream ${local_arch} baseline for module comparison"
  git clone --no-hardlinks "$src_dir" "$local_upstream_src" >/dev/null
  git -c advice.detachedHead=false -C "$local_upstream_src" checkout -q "v${version}"
  git -C "$local_upstream_src" reset --hard "v${version}" >/dev/null
  git -C "$local_upstream_src" clean -fdx >/dev/null

  (
    cd "$local_upstream_src"
    ./releng/make-binary.sh "$version" "$builder_tag"
  )

  if [ ! -s "$local_upstream_tar" ]; then
    echo "local upstream build did not produce expected tarball: $local_upstream_tar" >&2
    exit 1
  fi

  mkdir -p "$local_upstream_extract"
  tar -xzf "$local_upstream_tar" -C "$local_upstream_extract"
  local_upstream_initramfs="$(find_one "$local_upstream_extract" 'initramfs*.img')"
  zbm_lsinitrd "$builder_tag" "$local_upstream_initramfs" >"${report_dir}/local-upstream.lsinitrd"
  extract_modules "${report_dir}/local-upstream.lsinitrd" >"${report_dir}/local-upstream.modules"
  extract_binaries "${report_dir}/local-upstream.lsinitrd" >"${report_dir}/local-upstream.binaries"
  comm -23 "${report_dir}/local-upstream.modules" "${report_dir}/local.modules" >"${report_dir}/missing.local-upstream.modules"
  comm -23 "${report_dir}/local-upstream.binaries" "${report_dir}/local.binaries" >"${report_dir}/missing.local-upstream.binaries"
  local_upstream_missing_module_count="$(wc -l <"${report_dir}/missing.local-upstream.modules" | tr -d '[:space:]')"
fi

zbm_assert_core_listing "${report_dir}/local.lsinitrd" "local"
zbm_assert_core_listing "${report_dir}/official.lsinitrd" "official"

module_diff=0
binary_diff=0
diff -u "${report_dir}/official.modules" "${report_dir}/local.modules" >"${report_dir}/modules.diff" || module_diff=$?
diff -u "${report_dir}/official.binaries" "${report_dir}/local.binaries" >"${report_dir}/binaries.diff" || binary_diff=$?
comm -23 "${report_dir}/official.modules" "${report_dir}/local.modules" >"${report_dir}/missing.modules"
comm -23 "${report_dir}/official.binaries" "${report_dir}/local.binaries" >"${report_dir}/missing.binaries"
comm -13 "${report_dir}/official.modules" "${report_dir}/local.modules" >"${report_dir}/added.modules"
comm -13 "${report_dir}/official.binaries" "${report_dir}/local.binaries" >"${report_dir}/added.binaries"

print_diff_excerpt() {
  local label="$1"
  local path="$2"

  echo
  echo "${label} diff excerpt:"
  sed -n '1,220p' "$path"
}

missing_module_count="$(wc -l <"${report_dir}/missing.modules" | tr -d '[:space:]')"
missing_binary_count="$(wc -l <"${report_dir}/missing.binaries" | tr -d '[:space:]')"
added_module_count="$(wc -l <"${report_dir}/added.modules" | tr -d '[:space:]')"
added_binary_count="$(wc -l <"${report_dir}/added.binaries" | tr -d '[:space:]')"

if [ "$local_arch" != "$official_arch" ]; then
  echo "Cross-architecture module reports are informational only; kernel configs differ beyond architecture-specific paths"
  echo "Official ${official_arch} modules absent from local ${local_arch}: ${missing_module_count}"
  echo "Local ${local_arch} modules absent from official ${official_arch}: ${added_module_count}"
  if [ "$local_upstream_missing_module_count" -eq 0 ]; then
    echo "Local ${local_arch} module set preserves pristine upstream ${local_arch} modules"
  else
    echo "Local ${local_arch} module set is missing ${local_upstream_missing_module_count} pristine upstream ${local_arch} modules; see ${report_dir}/missing.local-upstream.modules" >&2
    print_diff_excerpt "Missing local-upstream module" "${report_dir}/missing.local-upstream.modules" >&2
  fi
elif [ "$module_diff" -eq 0 ]; then
  echo "Module set matches official ${official_asset_base}"
elif [ "$missing_module_count" -eq 0 ]; then
  echo "Local module set contains all official modules plus ${added_module_count} overlay additions"
else
  echo "Local module set is missing ${missing_module_count} official modules; see ${report_dir}/missing.modules" >&2
  print_diff_excerpt "Missing module" "${report_dir}/missing.modules" >&2
fi

if [ "$binary_diff" -eq 0 ]; then
  echo "Binary set matches official ${official_asset_base}"
elif [ "$missing_binary_count" -eq 0 ]; then
  echo "Local binary set contains all official binaries plus ${added_binary_count} overlay additions"
else
  echo "Local binary set is missing ${missing_binary_count} official binaries; see ${report_dir}/missing.binaries" >&2
  print_diff_excerpt "Missing binary" "${report_dir}/missing.binaries" >&2
fi

if { [ "$local_arch" = "$official_arch" ] && [ "$missing_module_count" -ne 0 ]; } || [ "$local_upstream_missing_module_count" -ne 0 ] || [ "$missing_binary_count" -ne 0 ]; then
  echo "Reports kept at ${report_dir}" >&2
  exit 1
fi

if [ "$module_diff" -ne 0 ] || [ "$binary_diff" -ne 0 ]; then
  echo "Full module diff: ${report_dir}/modules.diff"
  echo "Full binary diff: ${report_dir}/binaries.diff"
fi

if [ "$local_arch" != "$official_arch" ]; then
  echo "Local ${local_arch} build passes official ${official_asset_base} core and binary checks; module reports are informational"
else
  echo "Local ${local_arch} build preserves official ${official_asset_base} initramfs listings"
fi

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: build.sh [x86_64|aarch64|all ...]

Build the interactive keytest application and no-UI rEFInd driver for one or
more UEFI architectures. Defaults to x86_64.
EOF
  exit 0
fi

if [ -n "${CLANG:-}" ]; then
  clang=$CLANG
elif command -v clang >/dev/null 2>&1; then
  clang=$(command -v clang)
elif [ -x /opt/homebrew/opt/llvm/bin/clang ]; then
  clang=/opt/homebrew/opt/llvm/bin/clang
else
  echo "No clang found. Install clang or set CLANG." >&2
  exit 1
fi

lld_link=${LLD_LINK:-}

if [ -z "$lld_link" ]; then
  if command -v lld-link >/dev/null 2>&1; then
    lld_link=$(command -v lld-link)
  elif command -v ld.lld >/dev/null 2>&1; then
    lld_link="$(command -v ld.lld) -flavor link"
  elif [ -x /opt/homebrew/opt/lld/bin/lld-link ]; then
    lld_link=/opt/homebrew/opt/lld/bin/lld-link
  elif [ -x /opt/homebrew/opt/lld/bin/ld.lld ]; then
    lld_link="/opt/homebrew/opt/lld/bin/ld.lld -flavor link"
  else
    echo "No lld-link/ld.lld found. Install Homebrew lld or set LLD_LINK." >&2
    exit 1
  fi
fi

sha256_file() {
  local file=$1
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname "$file")" && sha256sum "$(basename "$file")" >"$(basename "$file").sha256sum")
  else
    (cd "$(dirname "$file")" && shasum -a 256 "$(basename "$file")" >"$(basename "$file").sha256sum")
  fi
}

build_arch() {
  local arch=$1
  local target boot_name driver_name out_dir red_zone_flags=()

  case "$arch" in
  x86_64 | amd64)
    arch=x86_64
    target=x86_64-unknown-windows
    boot_name=BOOTX64.EFI
    driver_name=homelab_fr_azerty_x64.efi
    red_zone_flags=(-mno-red-zone)
    ;;
  aarch64 | arm64)
    arch=aarch64
    target=aarch64-unknown-windows
    boot_name=BOOTAA64.EFI
    driver_name=homelab_fr_azerty_aa64.efi
    ;;
  *)
    echo "unsupported arch: $arch (expected x86_64, aarch64, or all)" >&2
    exit 1
    ;;
  esac

  out_dir="build/${arch}"
  mkdir -p "$out_dir"

  "$clang" \
    --target="$target" \
    -ffreestanding \
    -fshort-wchar \
    "${red_zone_flags[@]}" \
    -fno-stack-protector \
    -fno-builtin \
    -Wall \
    -Wextra \
    -Werror \
    -c hii_azerty_keytest.c \
    -o "${out_dir}/hii_azerty_keytest.obj"

  "$clang" \
    --target="$target" \
    -DKEYTEST_DRIVER_ONLY \
    -ffreestanding \
    -fshort-wchar \
    "${red_zone_flags[@]}" \
    -fno-stack-protector \
    -fno-builtin \
    -Wall \
    -Wextra \
    -Werror \
    -c hii_azerty_keytest.c \
    -o "${out_dir}/homelab_fr_azerty.obj"

  # shellcheck disable=SC2086  # LLD_LINK may intentionally contain "ld.lld -flavor link".
  $lld_link \
    /subsystem:efi_application \
    /entry:efi_main \
    /timestamp:0 \
    /nodefaultlib \
    "/out:${out_dir}/${boot_name}" \
    "${out_dir}/hii_azerty_keytest.obj"

  # shellcheck disable=SC2086  # LLD_LINK may intentionally contain "ld.lld -flavor link".
  $lld_link \
    /subsystem:efi_boot_service_driver \
    /entry:efi_main \
    /timestamp:0 \
    /nodefaultlib \
    "/out:${out_dir}/${driver_name}" \
    "${out_dir}/homelab_fr_azerty.obj"

  sha256_file "${out_dir}/${boot_name}"
  sha256_file "${out_dir}/${driver_name}"

  if [ "$arch" = "x86_64" ]; then
    cp "${out_dir}/${boot_name}" "build/${boot_name}"
    cp "${out_dir}/${driver_name}" "build/${driver_name}"
    cp "${out_dir}/${boot_name}.sha256sum" "build/${boot_name}.sha256sum"
    cp "${out_dir}/${driver_name}.sha256sum" "build/${driver_name}.sha256sum"
  fi

  echo "Built $(pwd)/${out_dir}/${boot_name}"
  echo "Built $(pwd)/${out_dir}/${driver_name}"
}

targets=("$@")
if [ "${#targets[@]}" -eq 0 ]; then
  targets=(x86_64)
fi
if [ "${#targets[@]}" -eq 1 ] && [ "${targets[0]}" = "all" ]; then
  targets=(x86_64 aarch64)
fi

for target in "${targets[@]}"; do
  build_arch "$target"
done

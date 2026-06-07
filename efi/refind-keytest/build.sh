#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

llvm_bin=${LLVM_BIN:-/opt/homebrew/opt/llvm/bin}
clang=${CLANG:-$llvm_bin/clang}
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

mkdir -p build

"$clang" \
  --target=x86_64-unknown-windows \
  -ffreestanding \
  -fshort-wchar \
  -mno-red-zone \
  -fno-stack-protector \
  -fno-builtin \
  -Wall \
  -Wextra \
  -Werror \
  -c hii_azerty_keytest.c \
  -o build/hii_azerty_keytest.obj

"$clang" \
  --target=x86_64-unknown-windows \
  -DKEYTEST_DRIVER_ONLY \
  -ffreestanding \
  -fshort-wchar \
  -mno-red-zone \
  -fno-stack-protector \
  -fno-builtin \
  -Wall \
  -Wextra \
  -Werror \
  -c hii_azerty_keytest.c \
  -o build/homelab_fr_azerty_x64.obj

# shellcheck disable=SC2086  # LLD_LINK may intentionally contain "ld.lld -flavor link".
$lld_link \
  /subsystem:efi_application \
  /entry:efi_main \
  /nodefaultlib \
  /out:build/BOOTX64.EFI \
  build/hii_azerty_keytest.obj

# shellcheck disable=SC2086  # LLD_LINK may intentionally contain "ld.lld -flavor link".
$lld_link \
  /subsystem:efi_application \
  /entry:efi_main \
  /nodefaultlib \
  /out:build/homelab_fr_azerty_x64.efi \
  build/homelab_fr_azerty_x64.obj

echo "Built $(pwd)/build/BOOTX64.EFI"
echo "Built $(pwd)/build/homelab_fr_azerty_x64.efi"

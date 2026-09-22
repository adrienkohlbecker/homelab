#!/bin/bash
# ZFSBootMenu early-setup hook: make kexec load Ubuntu's arm64 kernels.
#
# Two things stop kexec from booting an Ubuntu boot environment on aarch64:
#
# 1. Ubuntu's kernels drop the EFI parameters in /chosen, and then panic in
#    early paging, unless the DTB has linux,uefi-secure-boot. Ubuntu's EFI stub
#    writes it; the upstream stub of ZBM's own kernel does not. Regenerate the DTB
#    kexec derives from /sys/firmware/fdt with the property added and pass it to
#    every kexec load. kexec_file_load ignores --dtb, so loads go through
#    kexec_load: the wrapper drops -a (file syscall first) and forces
#    --kexec-syscall.
# 2. Since 26.04 Ubuntu ships vmlinuz as a PE image (systemd-stub layout) whose
#    .linux section is an EFI zboot image with a compressed arm64 Image inside.
#    kexec-tools cannot load that, so the wrapper cuts the payload out and
#    decompresses it first.
#
# kexec_load hands off through kexec-tools' purgatory, which SHA-256-verifies
# every loaded segment with the MMU off. Uncached, that takes ~30s on the
# Cortex-A72 CI hosts, so loads pass --no-checks. It must go on the load:
# ZBM's own -i on kexec -e comes too late to reach purgatory.
#
# See notes/zbm_aarch64_kexec_investigation.md.
set -euo pipefail

[ -r /sys/firmware/fdt ] || exit 0
cp /sys/firmware/fdt /run/zbm_kexec.dtb || exit 0
fdtput -t u /run/zbm_kexec.dtb /chosen linux,uefi-secure-boot 2 || exit 0

mv /usr/bin/kexec /usr/bin/kexec.real
cat >/usr/bin/kexec <<'WRAPPER'
#!/bin/bash
set -euo pipefail

# Print the path of an arm64 Image for $1: the file itself, or the payload of the
# zboot image inside a PE wrapper. Anything unrecognized is passed through.
unwrap_kernel() {
  local file=$1 out=/run/kexec_kernel_Image off base offset size type decompress
  [ "$(head -c 2 "$file")" = MZ ] || { echo "$file"; return; }

  # The zboot header starts 4 bytes into an "MZ" stub: "zimg", payload offset
  # and size (little-endian u32, offset relative to the stub), 8 reserved bytes,
  # then the compression name.
  off=$(grep -abo zimg "$file" || true)
  off=${off%%$'\n'*}
  off=${off%%:*}
  [ -n "$off" ] || { echo "$file"; return; }
  base=$((off - 4))
  offset=$(od -An -tu4 -j $((off + 4)) -N4 "$file")
  size=$(od -An -tu4 -j $((off + 8)) -N4 "$file")
  # head closing the pipe early makes tail exit on SIGPIPE, which pipefail
  # would otherwise turn into a failure.
  type=$(
    set +o pipefail
    tail -c +$((off + 21)) "$file" | head -c 4
  )

  case "$type" in
    zstd) decompress="zstd -dc" ;;
    gzip) decompress="gzip -dc" ;;
    *) echo "$file"; return ;;
  esac
  (
    set +o pipefail
    tail -c +$((base + offset + 1)) "$file" | head -c $((size)) | $decompress >"$out"
  )
  [ -s "$out" ] || { echo "$file"; return; }
  echo "$out"
}

args=()
load=
kernel=
for arg in "$@"; do
  case "$arg" in
    -l | --load) load=1 ;;
    -a) continue ;;
    -*) ;;
    *) kernel=${#args[@]} ;;
  esac
  args+=("$arg")
done

if [ -n "$load" ]; then
  [ -z "$kernel" ] || args[kernel]=$(unwrap_kernel "${args[kernel]}")
  exec /usr/bin/kexec.real --kexec-syscall --no-checks "${args[@]}" --dtb=/run/zbm_kexec.dtb
fi
exec /usr/bin/kexec.real "$@"
WRAPPER
chmod 0755 /usr/bin/kexec

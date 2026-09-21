#!/bin/bash
# ZFSBootMenu early-setup hook: make kexec hand Ubuntu kernels a DTB they accept.
#
# Ubuntu's arm64 kernels drop the EFI parameters in /chosen, and then panic in
# early paging, unless linux,uefi-secure-boot is present. Ubuntu's EFI stub
# writes it; the upstream stub of ZBM's own kernel does not. Regenerate the DTB
# kexec derives from /sys/firmware/fdt with the property added and pass it to
# every kexec load. kexec_file_load ignores --dtb, so loads go through
# kexec_load: the wrapper drops -a (file syscall first) and forces
# --kexec-syscall. See notes/zbm_aarch64_kexec_investigation.md.
set -euo pipefail

[ -r /sys/firmware/fdt ] || exit 0
cp /sys/firmware/fdt /run/zbm_kexec.dtb || exit 0
fdtput -t u /run/zbm_kexec.dtb /chosen linux,uefi-secure-boot 2 || exit 0

mv /usr/bin/kexec /usr/bin/kexec.real
cat >/usr/bin/kexec <<'WRAPPER'
#!/bin/bash
set -euo pipefail
args=()
load=
for arg in "$@"; do
  case "$arg" in
    -l | --load) load=1 ;;
    -a) continue ;;
  esac
  args+=("$arg")
done
if [ -n "$load" ]; then
  exec /usr/bin/kexec.real --kexec-syscall "${args[@]}" --dtb=/run/zbm_kexec.dtb
fi
exec /usr/bin/kexec.real "$@"
WRAPPER
chmod 0755 /usr/bin/kexec

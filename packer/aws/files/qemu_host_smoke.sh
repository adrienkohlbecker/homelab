#!/usr/bin/env bash
# Smoke-test a provisioned qemu-host image before Packer captures it.
#
#   qemu_host_smoke.sh kernel
#   qemu_host_smoke.sh toolchain
#   qemu_host_smoke.sh firmware <qemu-binary> <machine> <code.fd> <vars-template.fd>
#
# Promotion points every new CI host at the image, so the bake proves what cells
# depend on: the host came back on the fleet's GA kernel, the runner binary
# starts, the baked mise toolchain runs as the CI user, and the UEFI firmware
# boots under qemu to its boot manager. Build instances are not metal, so the
# firmware boot uses TCG.
set -euo pipefail

run_as_ci_user() {
  (cd /tmp && sudo -u ubuntu env -i HOME=/home/ubuntu MISE_DATA_DIR=/opt/mise \
    PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin mise exec -- "$@")
}

kernel() {
  local running
  running=$(uname -r)
  case "$running" in
  *-generic) ;;
  *)
    echo "qemu_host_smoke: running ${running}, expected the GA (-generic) kernel" >&2
    return 1
    ;;
  esac
  # The purge is what keeps grub from picking the higher-versioned AWS kernel
  # back up on the next boot; leftovers would make the capture a coin toss.
  if dpkg-query -W -f '${db:Status-Status} ${Package}\n' 'linux*aws*' 2>/dev/null |
    grep -q '^installed '; then
    echo "qemu_host_smoke: linux-aws packages are still installed" >&2
    return 1
  fi
  echo "==> running the GA kernel ${running} with no linux-aws packages left"
}

toolchain() {
  env -i PATH=/usr/bin:/bin gitlab-runner --version >/dev/null
  run_as_ci_user python3 --version
  run_as_ci_user uv --version
  run_as_ci_user aws --version
  run_as_ci_user yq --version
}

firmware() {
  local qemu=$1 machine=$2 code=$3 vars_template=$4 workdir pid
  workdir=$(mktemp -d)
  cp "$vars_template" "$workdir/vars.fd"
  "$qemu" -machine "$machine" -cpu max -accel tcg -m 512 -nographic -monitor none -net none \
    -serial "file:$workdir/serial.log" \
    -drive "if=pflash,format=raw,readonly=on,file=$code" \
    -drive "if=pflash,format=raw,file=$workdir/vars.fd" >/dev/null 2>&1 &
  pid=$!
  # edk2 prints BdsDxe lines once firmware initialization reaches boot
  # selection, whether or not a boot device exists.
  for _ in $(seq 1 90); do
    if grep -aq 'BdsDxe:' "$workdir/serial.log" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      rm -rf "$workdir"
      echo "==> $(basename "$code") reached the UEFI boot manager"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 2
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "qemu_host_smoke: $(basename "$code") did not reach the UEFI boot manager; serial output:" >&2
  cat "$workdir/serial.log" >&2 2>/dev/null || true
  rm -rf "$workdir"
  return 1
}

case "${1:-}" in
kernel) kernel ;;
toolchain) toolchain ;;
firmware)
  shift
  firmware "$@"
  ;;
*)
  echo "usage: $0 kernel | toolchain | firmware <qemu-binary> <machine> <code.fd> <vars-template.fd>" >&2
  exit 2
  ;;
esac

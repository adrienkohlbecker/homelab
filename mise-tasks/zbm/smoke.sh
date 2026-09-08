#!/usr/bin/env bash
#MISE description="ZBM smoke test: drive the serial menu and assert boot-environment handoff"
# Exercises the ZBM recovery loop end-to-end with no terminal:
# direct-boot the ZBM kernel + initrd against the box variant's packer image,
# wait for the menu on the captured serial log, select the default boot
# environment through the serial console, and assert the guest kexecs all the
# way to its login prompt.
#
#   mise run zbm:smoke                    # newest local zbm-build/<arch> tarball
#   mise run zbm:smoke <package-version>  # fetch that version from the GitLab
#                                         # package registry first (its arch
#                                         # suffix must match this host)
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
repo_root="$(zbm_repo_root)"
cd "$repo_root"

registry_url="https://gitlab.com/api/v4/projects/83079143/packages/generic/zfsbootmenu"
out_dir="${repo_root}/zbm-build/${arch}"

workdir="$(mktemp -d)"
launcher_pid=""
qmp_sock="${workdir}/qmp.sock"
serial_fifo="${workdir}/serial.in"
boot_log="${workdir}/serial.log"
mkfifo "$serial_fifo"
exec 3<>"$serial_fifo"

cleanup() {
  if [ -S "$qmp_sock" ]; then
    python3 - "$qmp_sock" <<'PY' 2>/dev/null || true
import json
import socket
import sys

s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
f = s.makefile("rw")
f.readline()
for cmd in ({"execute": "qmp_capabilities"}, {"execute": "quit"}):
    f.write(json.dumps(cmd) + "\n")
    f.flush()
    f.readline()
PY
  fi
  if [ -n "$launcher_pid" ]; then
    for _ in $(seq 1 15); do
      kill -0 "$launcher_pid" 2>/dev/null || break
      sleep 1
    done
    kill "$launcher_pid" 2>/dev/null || true
    wait "$launcher_pid" 2>/dev/null || true
  fi
  exec 3>&-
  rm -rf "$workdir"
}
trap cleanup EXIT INT TERM

version="${1:-}"
if [ -n "$version" ]; then
  case "$version" in
  *"-${arch}") ;;
  *)
    echo "package version ${version} does not end in -${arch}; its image cannot boot on this host" >&2
    exit 1
    ;;
  esac
  for name in "zfsbootmenu-${version}.tar.gz" "zfsbootmenu-${version}.tar.gz.sha256sum"; do
    curl -fsSL -o "${workdir}/${name}" "${registry_url}/${version}/${name}"
  done
  (cd "$workdir" && sha256sum -c "zfsbootmenu-${version}.tar.gz.sha256sum")
  tarball="${workdir}/zfsbootmenu-${version}.tar.gz"
else
  if ! tarball="$(zbm_latest_tarball "$out_dir" "$arch")"; then
    echo "no ${arch} tarball — run 'mise run zbm:build' first" >&2
    exit 1
  fi
fi
echo "Smoke-testing ${tarball}"

tar -xzf "$tarball" -C "$workdir" --no-same-owner
for member in cmdline initramfs-bootmenu.img; do
  if [ ! -f "${workdir}/${member}" ]; then
    echo "tarball is missing ${member}" >&2
    exit 1
  fi
done
base_cmdline=$(cat "${workdir}/cmdline")

HOMELAB_NET_BACKEND=slirp "${repo_root}/test/launch.py" \
  --machine box \
  --kernel "$workdir"/vmlin*-bootmenu \
  --initrd "${workdir}/initramfs-bootmenu.img" \
  --append "$base_cmdline loglevel=7 zbm.show" \
  --mem 2048 \
  --with-pflash \
  --qmp "$qmp_sock" \
  --no-ssh-wait \
  --foreground <"$serial_fifo" >"$boot_log" 2>&1 &
launcher_pid=$!

fail() {
  echo "$1" >&2
  echo "--- serial tail:" >&2
  tail -c 2000 "$boot_log" 2>/dev/null | LC_ALL=C sed -e $'s/\x1b\\[[0-9;?]*[a-zA-Z]//g' -e $'s/\r//g' >&2 || true
  exit 1
}

# Flake policy: every wait is bounded, and a dead launcher fails immediately.
wait_for() {
  local deadline=$1 desc=$2
  shift 2
  local start=$SECONDS
  until "$@" 2>/dev/null; do
    kill -0 "$launcher_pid" 2>/dev/null || fail "launch.py exited while waiting for ${desc}"
    [ $((SECONDS - start)) -lt "$deadline" ] || fail "timed out after ${deadline}s waiting for ${desc}"
    sleep 2
  done
}

menu_up() { LC_ALL=C grep -aq "Boot Environments" "$boot_log"; }
wait_for 180 "the ZFSBootMenu menu on the serial console" menu_up
echo "PASS: ZBM menu rendered and imported the box rpool"

printf '\r' >&3
boot_started() { LC_ALL=C grep -aq "Booting " "$boot_log"; }
wait_for 60 "ZFSBootMenu to start the selected boot environment" boot_started
echo "PASS: serial input selected the default boot environment"

# On aarch64 EDK2 the kexec handoff itself is a known upstream bug: the BE
# kernel starts and immediately panics with a misalignment complaint
# (notes/archive/zbm-aarch64-kexec-bug-report.md) — prod aarch64 boots via
# rEFInd EFI-stub instead. Accept that signature as proof the handoff fired;
# an upstream fix upgrades this run to the login-prompt assertion on its own.
booted() { LC_ALL=C grep -aqE "Welcome to Ubuntu|login:" "$boot_log"; }
known_misalign() { [ "$arch" = aarch64 ] && LC_ALL=C grep -aq "Kernel image misaligned at boot" "$boot_log"; }
handoff_done() { booted || known_misalign; }
wait_for 240 "the boot environment to come up after kexec" handoff_done
if booted; then
  echo "PASS: kexec handed off and the boot environment reached its login prompt"
else
  echo "PASS: kexec handed off; the BE kernel started and hit the known aarch64 EDK2 misalignment panic"
fi

echo "ZBM smoke test OK: ${tarball##*/}"

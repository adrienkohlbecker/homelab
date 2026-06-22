#!/usr/bin/env bash
#MISE description="Headless ZBM smoke test: boot a tarball against the box image and assert menu, dropbear, and BE handoff"
# Automates the interactive zbm:test loop end-to-end with no terminal:
# direct-boot the ZBM kernel + initrd against the box variant's packer image,
# wait for the menu on the captured serial log, check dropbear answers with
# the tarball's own host key, then select the default boot environment
# through a recovery SSH `zbm` session and assert the guest kexecs all the
# way to its login prompt.
#
#   mise run zbm:smoke                    # newest local zbm-build/<arch> tarball
#   mise run zbm:smoke <package-version>  # fetch that version from the GitLab
#                                         # package registry first (its arch
#                                         # suffix must match this host)
#
# Requires the operator's SSH agent: the recovery image bakes
# zbm/dropbear/authorized_keys, and selecting the BE happens over that SSH
# session because the serial console owns the on-screen menu (QMP sendkey
# would feed the unused VGA keyboard instead).
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
repo_root="$(zbm_repo_root)"
cd "$repo_root"

registry_url="https://gitlab.com/api/v4/projects/83079143/packages/generic/zfsbootmenu"
out_dir="${repo_root}/zbm-build/${arch}"

version="${1:-}"
if [ -n "$version" ]; then
  case "$version" in
  *"-${arch}") ;;
  *)
    echo "package version ${version} does not end in -${arch}; its image cannot boot on this host" >&2
    exit 1
    ;;
  esac
  mkdir -p "$out_dir"
  for name in "zfsbootmenu-${version}.tar.gz" "zfsbootmenu-${version}.tar.gz.sha256sum"; do
    curl -fsSL -o "${out_dir}/${name}" "${registry_url}/${version}/${name}"
  done
  (cd "$out_dir" && sha256sum -c "zfsbootmenu-${version}.tar.gz.sha256sum")
  tarball="${out_dir}/zfsbootmenu-${version}.tar.gz"
else
  if ! tarball="$(zbm_latest_tarball "$out_dir" "$arch")"; then
    echo "no ${arch} tarball — run 'mise run zbm:build' first" >&2
    exit 1
  fi
fi
echo "Smoke-testing ${tarball}"

workdir="$(mktemp -d)"
launcher_pid=""
qmp_sock="${workdir}/qmp.sock"

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
  rm -rf "$workdir"
}
trap cleanup EXIT INT TERM

tar -xzf "$tarball" -C "$workdir" --no-same-owner
for member in cmdline ssh_host_ed25519_key.pub initramfs-bootmenu.img; do
  if [ ! -f "${workdir}/${member}" ]; then
    echo "tarball is missing ${member}" >&2
    exit 1
  fi
done
base_cmdline=$(cat "${workdir}/cmdline")

# Must match launch.py's --ubuntu default (test/matrix.py DEFAULT_UBUNTU);
# passed explicitly so the serial log path below is correct by construction.
ubuntu=jammy
boot_log="test/out/box.${ubuntu}._launch.boot.ansi"
rm -f "$boot_log"
ports_file="${workdir}/hostfwds"

"${repo_root}/test/launch.py" \
  --machine box \
  --ubuntu "$ubuntu" \
  --kernel "$workdir"/vmlin*-bootmenu \
  --initrd "${workdir}/initramfs-bootmenu.img" \
  --append "$base_cmdline loglevel=7 zbm.show" \
  --mem 2048 \
  --with-pflash \
  --qmp "$qmp_sock" \
  --extra-hostfwd 222 \
  --write-hostfwds "$ports_file" \
  --no-ssh-wait >"${workdir}/launch.log" 2>&1 &
launcher_pid=$!

fail() {
  echo "$1" >&2
  echo "--- launch.log tail:" >&2
  tail -n 15 "${workdir}/launch.log" >&2 || true
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

wait_for 60 "the dropbear hostfwd allocation" test -s "$ports_file"
ssh_port=$(awk '$2 == 222 {print $1}' "$ports_file")

# macOS ssh-keyscan emits its `# host banner` comment on stdout, so match
# the key line by type instead of taking the output wholesale.
scan_hostkey() {
  ssh-keyscan -T 5 -p "$ssh_port" -t ed25519 127.0.0.1 2>/dev/null |
    awk '$2 == "ssh-ed25519" {print $2, $3}' >"${workdir}/scanned.pub"
  test -s "${workdir}/scanned.pub"
}
wait_for 60 "a dropbear host key on port ${ssh_port}" scan_hostkey
if ! diff <(awk '{print $1, $2}' "${workdir}/ssh_host_ed25519_key.pub") "${workdir}/scanned.pub"; then
  fail "dropbear host key does not match the tarball ssh_host_ed25519_key.pub"
fi
echo "PASS: dropbear answers with the tarball host key"

# The SSH-side zbm waits for the console menu instance to yield, then the
# carriage return boots the highlighted default BE; kexec drops the
# connection, which ends this pipeline. `|| true`: a dropped connection is
# the success path.
{
  sleep 8
  printf '\r'
  sleep 45
} | ssh -tt -p "$ssh_port" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
  -o ConnectTimeout=10 \
  root@127.0.0.1 zbm >"${workdir}/zbm-session.log" 2>&1 || true
if ! LC_ALL=C grep -aq "Booting " "${workdir}/zbm-session.log"; then
  fail "the recovery SSH zbm session never reported booting a BE"
fi
echo "PASS: recovery SSH session accepted the agent key and selected the default BE"

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

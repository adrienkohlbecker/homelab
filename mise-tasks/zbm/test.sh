#!/usr/bin/env bash
#MISE description="Boot the most recent ZBM build against the box variant's packer qcow2"
#MISE interactive=true
# Iterate on zbm/config.yaml + zbm/dracut.conf.d/* without re-running packer
# end-to-end: edit, `mise run zbm:build`, then `mise run zbm:test`. Picks the
# newest zfsbootmenu-v*-<arch>.tar.gz under zbm-build/<arch>/, extracts it
# into a temp dir, and direct-boots the kernel + initrd via test/launch.py
# in --foreground mode (mon:stdio, QMP socket, 9p share, EDK2 pflash). Quit
# with Ctrl-A,x or HMP `quit`.
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
# shellcheck disable=SC2012  # filenames here are well-known patterns, no spaces/newlines
# ls -t lists newest-first; take the first line without a `| head` pipe, whose
# early reader would SIGPIPE ls and trip pipefail.
tarball=$(ls -t "${MISE_CONFIG_ROOT}/zbm-build/${arch}"/zfsbootmenu-v*-"${arch}".tar.gz)
tarball=${tarball%%$'\n'*}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
tar -xzf "$tarball" -C "$tmp" --no-same-owner
# Read the base cmdline baked into the EFI bundle (written by zbm:build from
# config.yaml Kernel.CommandLine). Append test-only flags on top so the test
# VM starts with the exact same base parameters as a real boot.
base_cmdline=$(cat "$tmp/cmdline")
mkdir -p /tmp/zbm-extract

# Dropbear port is forwarded to a free host port allocated at launch time.
# --write-hostfwds writes "HOST_PORT 222" to a known path before qemu starts
# so it's readable from a second terminal once the serial console takes over.
zbm_ports_file="/tmp/zbm-dropbear-port"
echo "Dropbear SSH port → $zbm_ports_file (written before qemu starts)"
echo "  then: ssh -p \$(awk '{print \$1}' $zbm_ports_file) -o StrictHostKeyChecking=no root@127.0.0.1"

"${MISE_CONFIG_ROOT}/test/launch.py" \
  --machine box \
  --kernel "$tmp"/vmlin*-bootmenu \
  --initrd "$tmp/initramfs-bootmenu.img" \
  --append "$base_cmdline loglevel=7 zbm.show" \
  --mem 2048 \
  --with-pflash \
  --virtfs /tmp/zbm-extract:share \
  --qmp /tmp/qmp.sock \
  --extra-hostfwd 222 \
  --write-hostfwds "$zbm_ports_file" \
  --foreground \
  --display-window \
  --no-ssh-wait

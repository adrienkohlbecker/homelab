#!/usr/bin/env bash
#MISE description="Boot the most recent ZBM build against the box variant's packer qcow2"
# Iterate on zbm/config.yaml + zbm/dracut.conf.d/* without re-running packer
# end-to-end: edit, `mise run zbm:build`, then `mise run zbm:test`. Picks the
# newest zfsbootmenu-v*-<arch>.tar.gz under zbm-build/<arch>/, extracts it
# into a temp dir, and direct-boots the kernel + initrd via test/launch.py
# in --foreground mode (mon:stdio, QMP socket, 9p share, EDK2 pflash). Quit
# with Ctrl-A,x or HMP `quit`.
set -eu

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
# shellcheck disable=SC2012  # filenames here are well-known patterns, no spaces/newlines
tarball=$(ls -t "${MISE_CONFIG_ROOT}/zbm-build/${arch}"/zfsbootmenu-v*-"${arch}".tar.gz | head -1)
tmp=$(mktemp -d -t zbm-test)
trap 'rm -rf "$tmp"' EXIT INT TERM
tar -xzf "$tarball" -C "$tmp" --no-same-owner
mkdir -p /tmp/zbm-extract

"${MISE_CONFIG_ROOT}/test/launch.py" \
  --machine box \
  --kernel "$tmp"/vmlin*-bootmenu \
  --initrd "$tmp/initramfs-bootmenu.img" \
  --append 'loglevel=7 zbm.show' \
  --mem 8192 \
  --with-pflash \
  --virtfs /tmp/zbm-extract:share \
  --qmp /tmp/qmp.sock \
  --foreground \
  --no-ssh-wait

#!/bin/bash
# Hetzner image setup, run by chroot.sh inside the target root (INSTALL_TARGET
# = hetzner). Consumes $UBUNTU_NAME (exported by packer's shell provisioner)
# and the staged /var/tmp/hetzner tree from provision.sh.
set -euxo pipefail

hetzner_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# cloud-guest-utils ships growpart, used by hetzner_growpart.service below.
apt-get install --yes cloud-init cloud-guest-utils

# Install the Hetzner cloud-init drop-in this release's stock hcloud image
# ships (captured verbatim under packer/hetzner/). It carries the
# mirror.hetzner.com package_mirrors and the Hetzner module set, so our
# debootstrap'd cloud-init behaves like the stock image: apt_configure
# points sources.list.d at the Hetzner mirror on first boot. Its
# default_user is root, but terraform user_data's `users:` block replaces
# that list with `ak` (verified: ak is the sole login user, root locked).
# The 99-hetzner.cfg datasource pin below sorts last and wins.
install -m 0644 "$hetzner_dir/90-hetznercloud.cfg.$UBUNTU_NAME" \
  /etc/cloud/cloud.cfg.d/90-hetznercloud.cfg

# Pin the datasource so a fresh cloud-init (debootstrap'd, not the
# Hetzner-tuned stock image) finds Hetzner's metadata + user-data fast
# instead of probing the full list. Hetzner provides networking + user-data
# here, so cloud-init owns the netplan (provision.sh skipped its static one).
# VALIDATE on a throwaway cpx22: confirm `ak` is created and SSH works — if
# the Hetzner DS isn't detected (DMI mismatch), fall back to ConfigDrive/
# NoCloud or force ds=hetzner on the kernel cmdline. See notes.
install -m 0644 "$hetzner_dir/99-hetzner.cfg" /etc/cloud/cloud.cfg.d/99-hetzner.cfg

# Image ships at 60G but deploys onto cpx22's ~76G, leaving rpool's partition
# (p5, last on disk) short with the GPT backup header mid-disk. The preceding
# 40G Podman partition stays fixed while p5 consumes the added capacity.
# hetzner_growpart.service grows p5 (growpart relocates the backup header) and
# runs `zpool online -e` once on first boot — late + sentinel-gated so a
# failure can't wedge the root mount. autoexpand covers any later disk resize.
zpool set autoexpand=on rpool

install -m 0755 "$hetzner_dir/hetzner_growpart.sh" /usr/local/sbin/hetzner_growpart.sh
install -m 0644 "$hetzner_dir/hetzner_growpart.service" \
  /etc/systemd/system/hetzner_growpart.service
systemctl enable hetzner_growpart.service

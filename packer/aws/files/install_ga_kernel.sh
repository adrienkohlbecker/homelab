#!/usr/bin/env bash
# Put the qemu-host image on the release GA kernel before anything else is
# provisioned on it.
#
# The stock Ubuntu AMI boots linux-aws, a rolling flavour that does not track
# the release GA kernel: on noble it is 7.0 where linux-generic -- what the
# fixtures, lab, and the rest of the fleet run -- is 6.8. A CI host exercising
# a different kernel than the fleet has already cost us once, when the newer
# kernel started mediating AF_UNIX in AppArmor and the packaged passt profile
# had no rule for it. Grub boots the highest version it finds, so the AWS
# kernel has to be gone before the reboot, not merely outranked by policy.
set -euxo pipefail

sudo apt-get update -qq
# linux-image-generic depends on `linux-firmware | linux-firmware-minimal`, and
# unqualified apt takes the first: ~20 blob packages for GPUs, wireless and NICs
# no EC2 instance has. Naming the stub satisfies the alternation instead.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends linux-generic linux-firmware-minimal

# The kernel being purged is the running one, which linux-image's prerm
# defaults to refusing -- and under a noninteractive frontend it never gets to
# ask, so the default stands and the purge fails. Answer it up front: the
# replacement is installed and in grub already, so the host has a kernel to
# come back on.
echo 'linux-base linux-base/removing-running-kernel boolean false' |
  sudo debconf-set-selections

# Metas, versioned images, modules, headers, and tools alike; a rebuild on an
# image that already ships the GA kernel simply finds nothing to purge.
mapfile -t aws_kernel_packages < <(
  dpkg-query -W -f '${db:Status-Status} ${Package}\n' 'linux*aws*' 2>/dev/null |
    awk '$1 == "installed" { print $2 }'
)
if [ "${#aws_kernel_packages[@]}" -gt 0 ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "${aws_kernel_packages[@]}"
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y -qq

# The AWS image boots initrd-less, straight to a root device named by PARTUUID,
# which works only because linux-aws builds the Nitro drivers in. The GA kernel
# ships nvme and ena as modules, so root has to be found through an initramfs
# and the override has to go with the kernel that justified it. Fail loudly
# rather than hand grub a config that boots a kernel it cannot mount root for.
sudo rm -f /etc/default/grub.d/40-force-partuuid.cfg
if grep -rqs '^[^#]*GRUB_FORCE_PARTUUID' /etc/default/grub /etc/default/grub.d; then
  echo "install_ga_kernel: GRUB_FORCE_PARTUUID still set; initrd-less boot would panic" >&2
  exit 1
fi
sudo update-grub

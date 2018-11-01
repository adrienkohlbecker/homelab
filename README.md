homelab
==========

Encrypting the ssh key for ansible :
`openssl aes-256-cbc -a -salt -in files/ansible_rsa -out files/ansible_rsa.enc`

Decrypting the key :
`openssl aes-256-cbc -d -a -in files/ansible_rsa.enc -out files/ansible_rsa`

Provisioning a new host :

1. Add it to the `tosetup` group in `hosts.ini`. Specify the hostname to set and ansible_ssh_host for the ip
2. Run `ansible-playbook -i hosts.ini setup.yml --ask-pass --ask-sudo-pass`
3. Add the host to the applicable group(s), specify ansible_ssh_user & ansible_ssh_private_key_file
4. Run `ansible-playbook -i hosts.ini site.yml`

Default VM config (`ubuntu_base`)
- User deploy with authorized key
- Usual password
- Displays ip in login prompt

To build zfs modules manually (as root)

```
# dkms remove -m zfs -v 0.6.3 --all
# dkms remove -m spl -v 0.6.3 --all
# dkms add -m spl -v 0.6.3
# dkms add -m zfs -v 0.6.3
# dkms install -m spl -v 0.6.3
# dkms install -m zfs -v 0.6.3
```

Add to fstab for root btrfs volumes : noatime,nodiratime,compress,ssd

Check if VT-d is activated

`dmesg | grep -e DMAR -e IOMMU`

Configure zfs snapshots

```
zfs set com.sun:auto-snapshot=false tank
zfs set com.sun:auto-snapshot=true tank/legacy
zfs set com.sun:auto-snapshot:frequent=false tank
zfs set com.sun:auto-snapshot:hourly=false tank

zfs set com.sun:auto-snapshot=false backup
zfs set com.sun:auto-snapshot=false vms
```

Create datasets :
sudo zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD ...


Config ZFS

https://github.com/zfsonlinux/grub/issues/12
http://blog.ls-al.com/ubuntu-on-a-zfs-root-file-system-for-ubuntu-14-04/
https://github.com/zfsonlinux/pkg-zfs/wiki/HOWTO-install-Ubuntu-to-a-Native-ZFS-Root-Filesystem

# New user

groupadd -g 1000 adrien
useradd -g 1000 -u 1000 -s /bin/bash -m adrien
echo "adrien ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/ansible
mkdir /home/adrien/.ssh
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIINvXcGX/AGw8m0BQeQENMxsYbDMKpfCqcZZv5k5mVGf" > /home/adrien/.ssh/authorized_keys
chown -R adrien:adrien /home/adrien/.ssh
chmod 0600 /home/adrien/.ssh/authorized_keys
chmod 0700 /home/adrien/.ssh

# https://github.com/scaleway/kernel-tools#how-to-build-a-custom-kernel-module

# Determine versions
arch="$(uname -m)"
release="$(uname -r)"
upstream="${release%%-*}"
local="${release#*-}"

# Get kernel sources
mkdir -p /usr/src
wget -O "/usr/src/linux-${upstream}.tar.xz" "https://cdn.kernel.org/pub/linux/kernel/v4.x/linux-${upstream}.tar.xz"
tar xf "/usr/src/linux-${upstream}.tar.xz" -C /usr/src/
ln -fns "/usr/src/linux-${upstream}" /usr/src/linux
ln -fns "/usr/src/linux-${upstream}" "/lib/modules/${release}/build"

# Prepare kernel
zcat /proc/config.gz > /usr/src/linux/.config
printf 'CONFIG_LOCALVERSION="%s"\nCONFIG_CROSS_COMPILE=""\n' "${local:+-$local}" >> /usr/src/linux/.config
wget -O /usr/src/linux/Module.symvers "http://mirror.scaleway.com/kernel/${arch}/${release}/Module.symvers"
apt-get install -y libssl-dev # adapt to your package manager
make -C /usr/src/linux prepare modules_prepare

apt-get update
apt-get install -y zfsutils-linux
zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none tank /dev/nbd1 /dev/nbd2
zfs create -o mountpoint=/var/lib/docker tank/docker

#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

echo 'Cleanup bash history'
unset HISTFILE
[ -f /root/.bash_history ] && rm /root/.bash_history
[ -f /home/vagrant/.bash_history ] && rm /home/vagrant/.bash_history

echo 'Cleanup log files'
find /var/log -type f | while read f; do echo -ne '' >"$f"; done

apt-get -y autoremove

# Remove APT cache
apt-get clean -y
apt-get autoclean -y

rm -rf /tmp/* >/dev/null

# echo 'Whiteout swap'
# swappart=$(tail -n1 /proc/swaps | awk -F ' ' '{print $1}')
# swapsize=$(tail -n1 /proc/swaps | awk -F ' ' '{print $3}')
# swapoff "$swappart"
# dd if=/dev/zero of="$swappart" bs=1024 count="$swapsize" || true
# mkswap "$swappart"
# swapon "$swappart"

# for pool in $(zpool list -H -o name); do
#   zfs create -o compression=off -o dedup=off -o mountpoint=/tmp/tmp $pool/tmp
#   dd if=/dev/zero of=/tmp/tmp/whitespace bs=1024 || true
#   rm /tmp/tmp/whitespace
#   zfs destroy $pool/tmp
# done

# echo 'Whiteout /boot/efi'
# dd if=/dev/zero of=/boot/efi/whitespace bs=1024 || true
# rm /boot/efi/whitespace

# Sync to ensure that the delete completes before this moves on.
sync
sync
sync

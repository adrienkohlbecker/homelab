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

echo 'Whiteout swap'
swappart=$(tail -n1 /proc/swaps | awk -F ' ' '{print $1}')
swapoff "$swappart"
fallocate -l 1G "$swappart"
chmod 600 "$swappart"
mkswap "$swappart"
swapon "$swappart"

echo 'Whiteout root'
count=$(df --sync -kP / | tail -n1 | awk -F ' ' '{print $4}')
let count--
dd if=/dev/zero of=/tmp/whitespace bs=1024 count="$count" || true
rm /tmp/whitespace

echo 'Whiteout /boot'
count=$(df --sync -kP /boot | tail -n1 | awk -F ' ' '{print $4}')
let count--
dd if=/dev/zero of=/boot/whitespace bs=1024 count="$count" || true
rm /boot/whitespace

echo 'Whiteout /boot/grub'
count=$(df --sync -kP /boot/grub | tail -n1 | awk -F ' ' '{print $4}')
let count--
dd if=/dev/zero of=/boot/grub/whitespace bs=1024 count="$count" || true
rm /boot/grub/whitespace

# Sync to ensure that the delete completes before this moves on.
sync
sync
sync

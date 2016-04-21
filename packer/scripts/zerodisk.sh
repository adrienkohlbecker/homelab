#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

echo 'Whiteout root'
count=$(df --sync -kP / | tail -n1  | awk -F ' ' '{print $4}')
let count--
dd if=/dev/zero of=/tmp/whitespace bs=1024 count="$count" || true
rm /tmp/whitespace

echo 'Whiteout /boot'
count=$(df --sync -kP /boot | tail -n1 | awk -F ' ' '{print $4}')
let count--
dd if=/dev/zero of=/boot/whitespace bs=1024 count="$count" || true
rm /boot/whitespace

echo 'Whiteout swap'
swappart=$(tail -n1 /proc/swaps | awk -F ' ' '{print $1}')
swapoff "$swappart"
dd if=/dev/zero of="$swappart" || true
mkswap "$swappart"
swapon "$swappart"

# Sync to ensure that the delete completes before this moves on.
sync
sync
sync

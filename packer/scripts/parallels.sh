#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

# Bail if we are not running inside Parallels.
if [[ $(facter virtual) != "parallels" ]]; then
    exit 0
fi

# Install the Parallels Tools

mkdir -p /mnt/parallels
mount -o loop /home/vagrant/prl-tools.iso /mnt/parallels

/mnt/parallels/install --install-unattended-with-deps --progress

umount /mnt/parallels

rm -rf /home/vagrant/prl-tools.iso

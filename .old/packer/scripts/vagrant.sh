#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

# Update the box
apt-get -y update >/dev/null
apt-get -y install python3 python3-apt python3-pip # for ansible provisioner
apt-get -y upgrade >/dev/null

# change user id for vagrant as 1000 is the default and is used in the playbook
# usermod -u 999 vagrant
# groupmod -g 999 vagrant
# find / -user 1000 -exec chown -h 999 {} \;
# find / -group 1000 -exec chgrp -h 999 {} \;
# usermod -g 999 vagrant

# Set up sudo
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ubuntu

# Remove 5s grub timeout to speed up booting
echo 'GRUB_TIMEOUT=0' >> /etc/default/grub.d/99-custom.cfg
echo 'GRUB_CMDLINE_LINUX_DEFAULT=""' >> /etc/default/grub.d/99-custom.cfg

update-grub

# Installing vagrant keys
mkdir -pm 700 /home/ubuntu/.ssh
wget --no-check-certificate 'https://raw.githubusercontent.com/mitchellh/vagrant/master/keys/vagrant.pub' -O /home/ubuntu/.ssh/authorized_keys
chmod 0600 /home/ubuntu/.ssh/authorized_keys
chown -R ubuntu /home/ubuntu/.ssh

reboot
sleep 60

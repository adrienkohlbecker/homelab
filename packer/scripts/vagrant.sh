#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

# Update the box
apt-get -y update >/dev/null
apt-get -y install facter
apt-get -y upgrade >/dev/null

# Set up sudo
echo 'vagrant ALL=NOPASSWD:ALL' > /etc/sudoers.d/vagrant

# Installing vagrant keys
mkdir -pm 700 /home/vagrant/.ssh
wget --no-check-certificate 'https://raw.githubusercontent.com/mitchellh/vagrant/master/keys/vagrant.pub' -O /home/vagrant/.ssh/authorized_keys
chmod 0600 /home/vagrant/.ssh/authorized_keys
chown -R vagrant /home/vagrant/.ssh

reboot
sleep 60

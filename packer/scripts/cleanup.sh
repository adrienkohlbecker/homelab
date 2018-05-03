#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

echo 'Cleanup bash history'
unset HISTFILE
[ -f /root/.bash_history ] && rm /root/.bash_history
[ -f /home/vagrant/.bash_history ] && rm /home/vagrant/.bash_history

echo 'Cleanup log files'
find /var/log -type f | while read f; do echo -ne '' > "$f"; done;

apt-get -y autoremove

# Remove APT cache
apt-get clean -y
apt-get autoclean -y

rm -rf /tmp/* >/dev/null

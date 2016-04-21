#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eu
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

# for add-apt-repository
apt-get -y update >/dev/null
apt-get install -y software-properties-common >/dev/null

# Install ansible for provisioning
add-apt-repository -y ppa:ansible/ansible >/dev/null
apt-get -y update >/dev/null
apt-get -y install ansible >/dev/null

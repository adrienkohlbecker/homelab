#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

# Install ansible for provisioning
apt-get -y update >/dev/null
apt-get -y install ansible >/dev/null

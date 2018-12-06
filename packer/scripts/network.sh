#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

# Fix networking
sed -i "s/ens192/ens32/" /etc/netplan/01-netcfg.yaml

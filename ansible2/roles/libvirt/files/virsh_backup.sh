#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

for domain in $(virsh list --all --name); do
  virsh dumpxml "$domain" > "/var/lib/libvirt/images/$domain.xml"
done
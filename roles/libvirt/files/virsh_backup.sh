#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

for domain in $(virsh list --all --name); do
  virsh dumpxml "$domain" > "/var/lib/libvirt/images/$domain.xml"
done

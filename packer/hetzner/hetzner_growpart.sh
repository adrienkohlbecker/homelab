#!/bin/bash
set -euo pipefail

disk=/dev/sda
part=5

rc=0
growpart "$disk" "$part" || rc=$?
# growpart: 0 = resized, 1 = NOCHANGE (already full), >1 = real error.
if [ "$rc" -gt 1 ]; then
  exit "$rc"
fi

udevadm settle || true
zpool online -e rpool "${disk}${part}"

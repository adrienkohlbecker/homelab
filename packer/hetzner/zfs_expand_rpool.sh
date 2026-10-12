#!/bin/bash
# Grow rpool into its partition; see zfs_expand_rpool.service.
set -euo pipefail

# The pool's single vdev, by the path zpool reports (by-partuuid on this image).
vdev=$(zpool list -vHP rpool | awk 'NR == 2 { print $1 }')
zpool online -e rpool "$vdev"

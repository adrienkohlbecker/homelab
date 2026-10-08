#!/usr/bin/env bash
#MISE description="Remove leftovers of failed packer builds and test-harness runs; restore images an interrupted publish left parked"
set -euo pipefail
shopt -s nullglob

# build.sh's publish parks the previous version as .<source>.old-<pid> between
# its two renames. With the source published, a parked version is stale; with
# nothing published, it is the last good one.
for parked in "${HOMELAB_CI_DIR}"/*/.*.old-*; do
  name=${parked##*/}
  name=${name#.}
  published="${parked%/*}/${name%.old-*}"
  if [ -e "${published}" ]; then
    rm -rf "${parked}"
  else
    mv "${parked}" "${published}"
    echo "restored ${published}"
  fi
done
rm -rf "${HOMELAB_CI_DIR}"/.build-* "${HOMELAB_CI_DIR}"/tmp*

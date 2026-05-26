#!/usr/bin/env bash
#MISE description="Build packer image source(s) and verify they boot"
#MISE interactive=true
#USAGE arg "[sources]..." help="Source names from qemu.pkr.hcl to build; empty = all"
#USAGE flag "--ubuntu <name>" help="Ubuntu release codename" default="jammy"
#USAGE flag "--upstream" help="Pull apt packages and the cloud image from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# Linux: keep packer's ISO cache off the root FS; falls through to
# packer's default (./packer_cache in cwd) on Mac.
case "$(uname -s)" in
Linux) export PACKER_CACHE_DIR=/mnt/scratch/packer ;;
Darwin) ;;
*)
  echo "Unsupported OS: $(uname -s)" >&2
  exit 1
  ;;
esac

base="${QEMU_DIR}/${usage_ubuntu}"
mkdir -p "${base}"

# Build into a tmpdir at QEMU_DIR root so the previous good artifacts
# at ${base}/<source> stay intact while the new ones build. packer's
# install post-processor moves each per-source output into ${base};
# we just rmdir the (empty) tmpdir afterwards. On failure the tmpdir
# is left behind for inspection (cleanup via packer:clean).
tmp=$(mktemp -d "${QEMU_DIR}/.build-XXXXXX")

# Build -only filter when sources are specified. Packer parallelizes
# the matched sources internally (one VM per source, non-overlapping
# host_port and vnc_port ranges declared per source in qemu.pkr.hcl);
# without -only it builds every source.
only_args=()
if [ -n "${usage_sources:-}" ]; then
  only=""
  # shellcheck disable=SC2086  # word-splitting on usage_sources is the point
  for src in ${usage_sources}; do
    only+="${only:+,}qemu.${src}"
  done
  only_args=("-only=${only}")
fi

# --on-error=ask keeps the build VM up on failure so it can be SSH'd
# into for debugging. Dev-only: any non-interactive caller (CI, cron,
# scheduled rebuilds) would hang on the prompt — swap to
# --on-error=cleanup for those.
packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  --on-error=ask \
  -var "ubuntu_name=${usage_ubuntu}" \
  -var "upstream_mirrors=${usage_upstream:-false}" \
  -var "build_directory=${tmp}" \
  -var "output_directory=${base}" \
  "${only_args[@]}" \
  packer

rmdir "${tmp}"

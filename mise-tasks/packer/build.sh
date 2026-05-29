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

# Surface the qemu_net_wrapper shim's NIC-backend decision log (passt vs slirp,
# the passt command + DNS proxy, the netdev rewrite) plus passt's own --debug
# log. packer routes the shim's stderr through Go's logger, which it discards
# without PACKER_LOG, so the shim writes to QEMU_NET_WRAPPER_LOG instead. Point
# it at a sibling file *outside* any per-source output dir: packer deletes a
# failed source's output_directory before this EXIT trap runs, which would take
# a build-dir-derived log with it. Tailing on EXIT whatever the outcome makes a
# passt-path regression diagnosable straight from the CI job log, no PACKER_LOG
# rerun needed.
netlog=$(mktemp "${QEMU_DIR}/.netlog-XXXXXX")
export QEMU_NET_WRAPPER_LOG="${netlog}"
dump_net_logs() {
  local f
  if [ -s "${netlog}" ]; then
    echo "=== qemu_net_wrapper NIC-backend decision log ==="
    cat "${netlog}"
  fi
  for f in "${netlog}".passt-*; do
    [ -f "${f}" ] || continue
    echo "=== ${f##*/} (passt sidecar --debug log) ==="
    cat "${f}"
  done
  rm -f "${netlog}" "${netlog}".passt-*
}
trap dump_net_logs EXIT

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

# --on-error=ask keeps the failed build VM up so it can be SSH'd into
# for debugging — but only useful with a human at the terminal. A
# non-interactive caller (CI, cron, scheduled rebuilds) has no stdin to
# answer the prompt: packer reads EOF and then tears down every
# in-flight parallel build, so one source's failure kills its otherwise-
# healthy siblings. Fall back to cleanup there so the unaffected sources
# still finish and publish (the run still exits non-zero on the failure).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -var "ubuntu_name=${usage_ubuntu}" \
  -var "upstream_mirrors=${usage_upstream:-false}" \
  -var "build_directory=${tmp}" \
  -var "output_directory=${base}" \
  "${only_args[@]}" \
  packer

rmdir "${tmp}"

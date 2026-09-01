#!/usr/bin/env bash
#MISE description="Build packer image source(s) and verify they boot"
#MISE interactive=true
#USAGE arg "[sources]..." help="Source names from qemu.pkr.hcl to build; empty = all"
#USAGE complete "sources" run="printf 'box\nlab\npug\nhetzner\n'"
#USAGE flag "--ubuntu... <ubuntu>" help="Ubuntu release codename; repeat to build multiple releases" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\nresolute\n'"
#USAGE flag "--upstream" help="Pull apt packages and the cloud image from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
#USAGE flag "--no-publish" help="Build and verify-boot but skip the install (publish) step. Used by feature-branch CI to validate packer changes without overwriting master's published artifacts."
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail
# Group-write so every homelab_ci-group member can mutually delete each
# other's files in the shared /mnt/scratch/homelab_ci dir.
umask 002

# mise folds a repeated --ubuntu flag into one space-joined value; fan out by
# re-invoking this script once per release so the body below stays
# single-release (its own traps, tmpdir, and netlog per process).
read -r -a ubuntus <<<"${usage_ubuntu}"
if [ "${#ubuntus[@]}" -gt 1 ]; then
  for ubuntu in "${ubuntus[@]}"; do
    usage_ubuntu="${ubuntu}" bash "$0"
  done
  exit 0
fi

# Linux: keep packer's ISO cache off the root FS; falls through to
# packer's default (./packer_cache in cwd) on Mac.
case "$(uname -s)" in
Linux) export PACKER_CACHE_DIR="${HOMELAB_CI_DIR}/packer_cache" ;;
Darwin) ;;
*)
  echo "Unsupported OS: $(uname -s)" >&2
  exit 1
  ;;
esac

base="${HOMELAB_CI_DIR}/${usage_ubuntu}"
mkdir -p "${base}"

# Build into a tmpdir at HOMELAB_CI_DIR root so the previous good artifacts
# at ${base}/<source> stay intact while the new ones build. packer's
# install post-processor moves each per-source output into ${base};
# we just rmdir the (empty) tmpdir afterwards. On failure the tmpdir
# is left behind for inspection (cleanup via packer:clean).
tmp=$(mktemp -d "${HOMELAB_CI_DIR}/.build-XXXXXX")
# mktemp uses 0700 regardless of umask; restore the shared-workspace contract.
chmod 2770 "${tmp}"

# Upload the Git-visible working tree as one archive so chroot.sh can consume
# the same role-owned files Ansible installs. Ignored private clones, caches,
# build artifacts, and .git stay out; existing uncommitted files stay in so a
# local Packer build validates the working tree being developed.
source_archive="${tmp}/homelab-source.tar"
repo_root=$(git rev-parse --show-toplevel)
(
  cd "${repo_root}"
  git ls-files -z --cached --others --exclude-standard |
    while IFS= read -r -d '' source_path; do
      if [ -e "${source_path}" ] || [ -L "${source_path}" ]; then
        printf '%s\0' "${source_path}"
      fi
    done |
    tar --create --file="${source_archive}" --no-recursion --null --files-from=-
)

# Surface the qemu_net_wrapper shim's NIC-backend decision log (passt vs slirp,
# the passt command + advertised DNS, the netdev rewrite) plus passt's own startup
# banner. packer routes the shim's stderr through Go's logger, which it discards
# without PACKER_LOG, so the shim writes to QEMU_NET_WRAPPER_LOG instead. Keep
# it in system temporary storage: packer can delete a failed source's output
# directory before the ERR trap dumps the log. Successful runs remove it.
netlog=$(mktemp "${TMPDIR:-/tmp}/homelab-packer-netlog.XXXXXX")
export QEMU_NET_WRAPPER_LOG="${netlog}"
dump_net_logs() {
  local f
  if [ -s "${netlog}" ]; then
    echo "=== qemu_net_wrapper NIC-backend decision log ==="
    cat "${netlog}"
  fi
  for f in "${netlog}".passt-*; do
    [ -f "${f}" ] || continue
    echo "=== ${f##*/} (passt sidecar startup banner) ==="
    grep -v '^Failed to send .* bytes to syslog$' "${f}" || true
  done
}
trap 'dump_net_logs; rm -f "${netlog}" "${netlog}".passt-*' ERR
trap 'rm -f "${netlog}" "${netlog}".passt-*' EXIT

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

publish=true
if [ "${usage_no_publish:-false}" = "true" ]; then
  publish=false
fi

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -var "ubuntu_name=${usage_ubuntu}" \
  -var "upstream_mirrors=${usage_upstream:-false}" \
  -var "publish=${publish}" \
  -var "build_directory=${tmp}" \
  -var "output_directory=${base}" \
  "${only_args[@]}" \
  packer

if [ "${publish}" = "true" ]; then
  rm "${source_archive}"
  rmdir "${tmp}"
else
  rm -rf "${tmp}"
fi

#!/usr/bin/env bash
#MISE description="Build packer image source(s) and verify they boot"
#MISE interactive=true
#USAGE arg "[sources]..." help="Source names from qemu.pkr.hcl to build; empty = all"
#USAGE complete "sources" run="printf 'lab\nhetzner\n'"
#USAGE flag "--ubuntu... <ubuntu>" help="Ubuntu release codename; repeat to build multiple releases" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--upstream" help="Pull apt packages and the cloud image from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
#USAGE flag "--no-publish" help="Build and verify-boot without replacing the published test fixtures. Useful for safe local Packer validation."
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail
# Group-write so every homelab_ci-group member can mutually delete each
# other's files in the shared /mnt/scratch/homelab_ci dir.
umask 002

# mise folds a repeated --ubuntu flag into one space-joined value; fan out by
# re-invoking this script once per release so the body below stays
# single-release (its own tmpdir per process).
read -r -a ubuntus <<<"${usage_ubuntu}"
if [ "${#ubuntus[@]}" -gt 1 ]; then
  for ubuntu in "${ubuntus[@]}"; do
    usage_ubuntu="${ubuntu}" bash "$0"
  done
  exit 0
fi

# Linux: keep packer's ISO cache off the root FS; falls through to
# packer's default (./packer_cache in cwd) on Mac. Linux builders keep raw
# disks because ZFS already provides CoW and zstd compression; APFS has no
# filesystem-level compression, so Mac ships zstd-compressed qcow2.
case "$(uname -s)" in
Linux)
  export PACKER_CACHE_DIR="${HOMELAB_CI_DIR}/packer_cache"
  image_format=raw
  ;;
Darwin) image_format=qcow2 ;;
*)
  echo "Unsupported OS: $(uname -s)" >&2
  exit 1
  ;;
esac

base="${HOMELAB_CI_DIR}/${usage_ubuntu}"
mkdir -p "${base}"

# Build into a tmpdir at HOMELAB_CI_DIR root so the previous good artifacts
# at ${base}/<source> stay intact while the new ones build. finalize moves
# each verified per-source output into ${base}. On failure the tmpdir is left
# behind for inspection (cleanup via packer:clean).
tmp=$(mktemp -d "${HOMELAB_CI_DIR}/.build-XXXXXX")
# mktemp uses 0700 regardless of umask; restore the shared-workspace contract.
chmod 2770 "${tmp}"

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

# rename(2), never mv: mv would move the build inside a directory that another
# build published first, instead of failing.
rename() { python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$1" "$2"; }

# Swap a verified build in with two renames, deleting the previous version only
# once it is out of the way; the harness links a version only after checking
# its path still names it (test/machine.py link_packer_artifacts). A concurrent
# publish of the same source makes the second rename fail and leaves its
# version published; any other failure puts the previous version back. An
# interrupt between the renames leaves it parked as .<source>.old-<pid>, which
# packer:clean removes.
publish() {
  local build_dir=$1 published=$2
  local old="${published%/*}/.${published##*/}.old-$$"

  # packer creates directories 0755 and qemu-img disks 0644, which narrows
  # the shared directory's default ACL mask; restore group write so the other
  # homelab_ci identity can replace this tree on a later publish, and hardlink
  # its disks (fs.protected_hardlinks requires write access to a file another
  # user owns).
  chmod -R g+rwX "${build_dir}"
  if [ -e "${published}" ]; then
    rename "${published}" "${old}"
  fi
  if ! rename "${build_dir}" "${published}"; then
    if [ -e "${published}" ]; then
      rm -rf "${old}"
      echo "publish of ${published} lost a race with another build; rerun packer:build" >&2
    elif [ -e "${old}" ]; then
      rename "${old}" "${published}"
      echo "publish of ${published} failed; restored the previous version" >&2
    fi
    return 1
  fi
  rm -rf "${old}"
}

# Turn one built source into a published fixture: drop the cloud-image OS disk
# (packer-ubuntu; provision.sh installs onto packer-ubuntu-1..N), give the
# rest their format suffix, prove a qemu fixture boots, compress on Mac, and
# atomically swap it in.
finalize() {
  local source=$1
  local build_dir="${tmp}/${source}" disk
  local vcpus_args=()

  rm "${build_dir}/packer-ubuntu"
  for disk in "${build_dir}"/packer-ubuntu-*; do
    mv "${disk}" "${disk}.${image_format}"
  done

  if [ "${source}" != hetzner ]; then
    # Stock QEMU's HVF never applies the reset state on PSCI CPU_ON, so a
    # kernel that ZFSBootMenu kexecs into cannot bring its secondary CPUs
    # online; a Mac builder verifies on one vCPU unless it runs the patched
    # qemu-hvf build.
    if [ "$(uname -s)" = Darwin ] && [[ "$(command -v qemu-system-aarch64)" != */qemu-hvf/* ]]; then
      vcpus_args=(--vcpus 1)
    fi
    test/launch.py \
      --machine "${source}" \
      --ubuntu "${usage_ubuntu}" \
      --exit-after-ready \
      --image-dir "${build_dir}" \
      ${vcpus_args[@]+"${vcpus_args[@]}"}
  fi

  if [ "${image_format}" = qcow2 ]; then
    for disk in "${build_dir}"/packer-ubuntu-*.qcow2; do
      echo "==> compressing ${disk##*/}"
      qemu-img convert -W -c -O qcow2 -o compression_type=zstd "${disk}" "${disk}.tmp"
      mv "${disk}.tmp" "${disk}"
    done
  fi

  if [ "${usage_no_publish:-false}" = true ]; then
    echo "==> Skipping publish of ${source} (--no-publish)"
  else
    publish "${build_dir}" "${base}/${source}"
  fi
}

packer_status=0
packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -var "host_arch=$(uname -m)" \
  -var "host_os=$(uname -s)" \
  -var "image_format=${image_format}" \
  -var "ubuntu_name=${usage_ubuntu}" \
  -var "upstream_mirrors=${usage_upstream:-false}" \
  -var "build_directory=${tmp}" \
  "${only_args[@]}" \
  packer || packer_status=$?

# The manifest lists only the sources that built successfully, so a source
# that fails to build never reaches finalize while its siblings still publish.
# finalize itself fails fast: a source that fails its boot check or publish
# stops the loop, and later sources stay unpublished in the kept tmpdir until
# a rerun. Read the manifest before looping: a failing $(...) in the for list
# would leave the loop empty and fall through to the cleanup below.
manifest="${tmp}/packer-manifest.json"
if [ -f "${manifest}" ]; then
  built_sources=$(yq -r '.builds[].custom_data.source' "${manifest}")
  for built in ${built_sources}; do
    finalize "${built}"
  done
fi
if [ "${packer_status}" -ne 0 ]; then
  exit "${packer_status}"
fi
rm -rf "${tmp}"

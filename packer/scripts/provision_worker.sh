#!/bin/bash
# Provision a Hetzner Cloud CI worker image. Installs everything the
# test harness, lint, and packer-build need directly on the host (no
# container). Mirrors the CI Dockerfile's package set but runs natively
# so workflow jobs execute in the shell, not inside a container image.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

# Retry helper for transient apt failures.
apt_update() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if apt-get update -o APT::Update::Error-Mode=any; then
      return 0
    fi
    echo "apt-get update attempt ${attempt} failed; retrying in $((attempt * 5))s" >&2
    sleep "$((attempt * 5))"
  done
  echo "apt-get update failed after 5 attempts" >&2
  return 1
}

echo 'Acquire::Retries "3";' >/etc/apt/apt.conf.d/80-retries
echo 'Acquire::Retries::Delay "true";' >>/etc/apt/apt.conf.d/80-retries

apt_update
apt-get upgrade -y

# --- Strip bloat from the stock image ---
# snapd: 5s boot penalty + 100 MB RSS, nothing in CI needs it. Pin to
# prevent apt from reseeding it as a recommends dep.
apt-get purge -y --auto-remove snapd squashfs-tools || true
cat >/etc/apt/preferences.d/no-snapd <<'APT'
Package: snapd
Pin: release *
Pin-Priority: -1
APT

# unattended-upgrades + apt daily timers: background apt-get update/
# upgrade races the CI apt calls and holds dpkg locks at the worst time.
apt-get purge -y --auto-remove unattended-upgrades || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

# ubuntu-advantage-tools (now ubuntu-pro-client): ESM nag + cloud attach
# probe at boot, neither useful on a throwaway CI worker.
apt-get purge -y --auto-remove ubuntu-advantage-tools ubuntu-pro-client 2>/dev/null || true

# motd-news: phones home to Ubuntu on login, adds ~1s to first SSH.
systemctl disable --now motd-news.timer 2>/dev/null || true
chmod -x /etc/update-motd.d/* 2>/dev/null || true

# --- Core packages ---
# qemu-system-x86 + qemu-utils: boot test VMs with KVM acceleration
# ovmf: UEFI firmware for the test VMs
# openssh-client: harness talks to guests via SSH
# xorriso + cloud-image-utils: minimal variant's seed ISO
# passt: guest NIC backend (replaces libslirp, avoids UDP drops)
# nodejs: GitHub Actions JS actions (checkout, upload-artifact, etc.)
# python3-yaml: mise-tasks/ci scripts
# build-essential: any wheel that needs compilation
# curl, jq, git, unzip: general tooling
# netcat-openbsd: UDP probes in _verify
apt-get install -y --no-install-recommends \
  ca-certificates curl git jq xz-utils unzip gpg apt-transport-https \
  qemu-system-x86 qemu-utils ovmf \
  openssh-client coreutils \
  netcat-openbsd \
  passt \
  xorriso cloud-image-utils \
  python3-yaml \
  nodejs \
  build-essential

# --- mise (tool version manager) ---
MISE_GPG_FPR="24853EC9F655CE80B48E6C3A8B81C9D17413A06D"
install -dm 755 /etc/apt/keyrings
curl -fsSL https://mise.jdx.dev/gpg-key.pub |
  gpg --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg
gpg --with-colons --import-options show-only --import /etc/apt/keyrings/mise-archive-keyring.gpg 2>/dev/null |
  grep -q "fpr:::::::::${MISE_GPG_FPR}:" || {
  echo "mise GPG key fingerprint mismatch — expected ${MISE_GPG_FPR}" >&2
  exit 1
}
echo "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main" \
  >/etc/apt/sources.list.d/mise.list
apt-get update
apt-get install -y --no-install-recommends mise

# mise data + shims on PATH for all users.
export MISE_DATA_DIR=/opt/mise
export PATH="/opt/mise/shims:${PATH}"

# uv cache in a fixed location (not under any user's $HOME).
export UV_CACHE_DIR=/opt/uv-cache
export UV_LINK_MODE=copy

# --- Install tools via mise ---
# The repo's mise.toml, pyproject.toml, uv.lock were uploaded by packer's
# file provisioner to /tmp/. Use them to install python, uv, opentofu,
# packer, shellcheck, shfmt, tflint at the pinned versions.
cd /tmp
mise trust
if [ -n "${MISE_GITHUB_TOKEN:-}" ]; then
  { set +x; } 2>/dev/null
  GITHUB_TOKEN="${MISE_GITHUB_TOKEN}" mise install
  set -x
else
  mise install
fi

# Warm uv's wheel cache so per-run `uv sync` resolves in seconds.
mise exec -- uv sync --frozen
rm -rf /tmp/.venv

# Global mise config so shims resolve from any CWD (same as Dockerfile).
mkdir -p /etc/mise
awk '/^\[tools\]/{p=1; print; next} /^\[/{p=0} p' /tmp/mise.toml \
  >/etc/mise/config.toml

# --- Packer plugins ---
# Pre-install the qemu + external plugins declared in qemu.pkr.hcl.
export PACKER_PLUGIN_PATH=/opt/packer/plugins
mkdir -p /tmp/packer_init
cp /tmp/qemu.pkr.hcl /tmp/packer_init/
packer init /tmp/packer_init
rm -rf /tmp/packer_init

# --- GitHub Actions runner ---
# Pre-install the actions/runner binary so the boot-time provisioning
# only needs to configure + register, not download ~500 MB.
# Version + sha256 read from the Ansible role's vars (single source of
# truth, uploaded by the packer file provisioner).
read -r RUNNER_URL RUNNER_SHA256 < <(python3 -c '
import yaml, sys
d = yaml.safe_load(open(sys.argv[1]))["github_runner_archive"]["x86_64"]
print(d["url"], d["sha256"])
' /tmp/github_runner_vars.yml)
mkdir -p /opt/actions-runner
cd /opt/actions-runner
curl -fL --retry 3 -o runner.tar.gz "$RUNNER_URL"
echo "${RUNNER_SHA256}  runner.tar.gz" | sha256sum -c -
tar xzf runner.tar.gz
rm runner.tar.gz
./bin/installdependencies.sh
cd /

# --- System tuning for nested virt ---
# Enable KVM nested virtualization (CCX instances expose /dev/kvm).
# The module may not be loaded during the packer build (cx22 is shared
# vCPU, no /dev/kvm), but the config will take effect on CCX at boot.
# Cover both vendors — Hetzner CCX is predominantly AMD EPYC but Intel
# is possible on shared-vCPU types.
mkdir -p /etc/modprobe.d
cat >/etc/modprobe.d/kvm-nested.conf <<'KVM'
options kvm_intel nested=1
options kvm_amd nested=1
KVM

# Bump inotify limits for parallel qemu + runner processes.
cat >/etc/sysctl.d/99-ci-worker.conf <<'SYSCTL'
fs.inotify.max_user_instances = 512
fs.inotify.max_user_watches = 524288
SYSCTL

# --- Scratch directory ---
# CI workdirs land here; created at image time so cloud-init doesn't
# need to. On a CCX33, this is on the root ext4 filesystem (NVMe).
mkdir -p /mnt/scratch/qemu
mkdir -p /mnt/ci_scratch

# --- Pre-seed packer's ISO cache ---
# Packer caches ISOs at PACKER_CACHE_DIR/<key>.iso where key =
# sha1("sha256:" + image_sha256). Pins come from ubuntu_images.json
# (shared with qemu.pkr.hcl). Pre-downloading saves ~600-900 MB per
# release on every ephemeral worker.
PACKER_CACHE=/mnt/scratch/packer
mkdir -p "$PACKER_CACHE"
packer_cache_seed() {
  local codename="$1" snapshot="$2" sha256="$3"
  local url="https://cloud-images.ubuntu.com/${codename}/${snapshot}/${codename}-server-cloudimg-amd64.img"
  local key
  key=$(echo -n "sha256:${sha256}" | sha1sum | awk '{print $1}')
  local dest="${PACKER_CACHE}/${key}.iso"
  if [ -f "$dest" ]; then return 0; fi
  echo "==> Pre-seeding packer cache: ${codename} (${snapshot}) -> ${key}.iso"
  curl -fL --retry 3 -o "${dest}.tmp" "$url"
  echo "${sha256}  ${dest}.tmp" | sha256sum -c -
  mv "${dest}.tmp" "$dest"
}

# --- Pre-cache minimal cloud images ---
# The `cleanup:minimal` (and any other :minimal) test cells download the
# Ubuntu minimal cloud image on first run (~400 MB each). Pre-caching
# at image-build time saves that download from every ephemeral worker.
# Path must match test/machine.py _ensure_minimal_cloudimg: <imagedir>/cloud-images/.
CLOUD_CACHE=/mnt/scratch/qemu/cloud-images
mkdir -p "$CLOUD_CACHE"

# Both caches are driven from ubuntu_images.json (uploaded by the
# packer file provisioner alongside this script).
IMAGES_JSON=/tmp/ubuntu_images.json
for codename in $(jq -r 'keys[]' "$IMAGES_JSON"); do
  version=$(jq -r ".\"${codename}\".version" "$IMAGES_JSON")
  snapshot=$(jq -r ".\"${codename}\".snapshot" "$IMAGES_JSON")
  sha256=$(jq -r ".\"${codename}\".sha256_amd64" "$IMAGES_JSON")

  packer_cache_seed "$codename" "$snapshot" "$sha256"

  img="ubuntu-${version}-minimal-cloudimg-amd64.img"
  if [ ! -f "${CLOUD_CACHE}/${img}" ]; then
    echo "==> Pre-caching ${img}"
    curl -fL --retry 3 -o "${CLOUD_CACHE}/${img}.tmp" \
      "https://cloud-images.ubuntu.com/minimal/releases/${codename}/release/${img}"
    mv "${CLOUD_CACHE}/${img}.tmp" "${CLOUD_CACHE}/${img}"
  fi
done

# --- Trim cloud-init to the minimum ---
# The worker only needs cloud-init to create the runner user + inject
# the SSH key on first boot. Disable modules that phone home, probe
# metadata services we don't have, or run package operations.
mkdir -p /etc/cloud/cloud.cfg.d
cat >/etc/cloud/cloud.cfg.d/99-ci-worker.cfg <<'CLOUDINIT'
datasource_list: [ Hetzner, ConfigDrive, NoCloud, None ]
# Skip slow metadata probes and package ops at boot.
cloud_init_modules:
  - migrator
  - seed_random
  - write_files
  - growpart
  - resizefs
  - set_hostname
  - update_hostname
  - users_groups
  - ssh
cloud_config_modules:
  - runcmd
  - ssh_import_id
cloud_final_modules: []
CLOUDINIT

# --- Environment for all users ---
# /etc/environment is the single source of truth — read by PAM (login
# shells) and by the GitHub runner's runsvc.sh (the primary entry point
# for CI step processes). /etc/profile.d re-exports for interactive
# login shells; non-login shells inherit from the runner or from PAM.
cat >/etc/environment <<'ENVFILE'
MISE_DATA_DIR=/opt/mise
PATH="/opt/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UV_CACHE_DIR=/opt/uv-cache
UV_LINK_MODE=copy
PACKER_PLUGIN_PATH=/opt/packer/plugins
PACKER_CACHE_DIR=/mnt/scratch/packer
ENVFILE

# /etc/profile.d for interactive login shells — source /etc/environment
# rather than duplicating the values.
cat >/etc/profile.d/ci-worker.sh <<'PROFILE'
set -a
# shellcheck disable=SC1091
. /etc/environment
set +a
PROFILE

# --- Cleanup (hcloud image size reduction) ---
# https://developer.hashicorp.com/packer/integrations/hetznercloud/hcloud/latest/components/builder/hcloud
# Every freed byte here shrinks the snapshot and speeds up instance creation.

# Keep /var/lib/apt/lists/ populated so a runtime apt-get install can
# skip the ~5s apt-get update (lists go stale after ~24h, but these are
# ephemeral instances). Drop only the downloaded .deb cache + autoremove
# orphans from the purge steps above.
apt-get -y autopurge
apt-get -y clean

rm -f /tmp/mise.toml /tmp/pyproject.toml /tmp/uv.lock /tmp/qemu.pkr.hcl /tmp/ubuntu_images.json /tmp/github_runner_vars.yml

# Truncate logs accumulated during provisioning. Don't delete the files
# (some daemons reopen by name, not fd) — just zero them.
journalctl --flush
journalctl --rotate --vacuum-time=0
find /var/log -type f -exec truncate --size 0 {} +
find /var/log -type f -name '*.[1-9]' -delete
find /var/log -type f -name '*.gz' -delete

# Remove SSH host keys — cloud-init regenerates them on first boot,
# giving each instance a unique host key.
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub

# Blank machine-id so each instance gets a unique one.
: >/etc/machine-id
if [ -e /var/lib/dbus/machine-id ]; then
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
fi

# Clean cloud-init state so it re-runs on the next boot. Keep --configs
# out: our 99-ci-worker.cfg must survive into the snapshot. machine-id
# is already blanked above (empty file, not cloud-init's "uninitialized"
# sentinel).
cloud-init clean --logs --seed

# TRIM the filesystem so the hypervisor knows which blocks are free.
# This is the single biggest snapshot-size win: a 20 GB disk with 8 GB
# used snapshots as ~8 GB instead of ~20 GB.
fstrim --all || true
sync

echo "==> CI worker image provisioned successfully"

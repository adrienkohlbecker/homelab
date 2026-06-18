#!/usr/bin/env bash
#MISE description="Bake the CI coordinator's Hetzner snapshot: stock Ubuntu + dockerd + the operator key, the boot image the fleeting docker-autoscaler clones per pipeline (notes/ci_aws_test_cells.md). No container images are baked in -- the coordinator pulls its job image + gitlab-runner-helper at runtime from registry.gitlab.com (its reserved egress IP pulls reliably; the private CI image authenticates with the job's CI_JOB_TOKEN). Rebake only when docker or the base OS changes."
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

ubuntu="${usage_ubuntu}"
case "$ubuntu" in
noble) base_image="ubuntu-24.04" ;;
resolute) base_image="ubuntu-26.04" ;;
*)
  echo "unsupported ubuntu: $ubuntu (expected noble or resolute)" >&2
  exit 1
  ;;
esac

# The temp build box is throwaway; size is irrelevant to the snapshot. cpx22's
# 80G disk is <= every fleeting target (cx53 is 320G), so a server cloned from
# this snapshot grows root via cloud-init growpart -- a larger base would cap
# which server_types could launch it.
TYPE="cpx22"
SERVER="packer-ci-coordinator"

# This flow is self-contained rather than sourcing _hetzner_rescue.sh, whose
# lifecycle is rescue-mode + dd-stream specific (and drives the live fox bake):
# here it is a normal boot + provision + snapshot. Every hcloud verb below
# blocks on its server action (create/poweroff/create-image/rebuild/poweron all
# poll to completion), so the server is in the requested state by the time the
# call returns -- the only waits left are for sshd, not an hcloud action.

# IdentitiesOnly: offer only the ephemeral key, not a forwarded agent's
# identities (else sshd hits MaxAuthTries first). NODE_IP/KEY/KNOWN are set
# during setup.
ssh_node() { ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=5 "root@$NODE_IP" "$@"; }

# True when host:22 has a live sshd, whether or not it accepts our key. rc 0 =
# our key authorized; an auth-failure message = sshd answered but rejected us;
# both prove the OS booted far enough to start sshd. Refused/timed-out = not
# (yet) booted. Leans on ssh's own ConnectTimeout (no nc/timeout binary).
node_sshd_up() { # IP
  local out
  out=$(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 -o PreferredAuthentications=publickey \
    "root@$1" true 2>&1) && return 0
  printf '%s' "$out" | grep -qiE 'permission denied|authentication failure|too many authentication'
}

KEYDIR="" SNAP_TO_CLEAN=""

cleanup() {
  echo "==> deleting temp server $SERVER"
  hcloud server delete "$SERVER" >/dev/null 2>&1 || true
  [ -n "${KEYNAME:-}" ] && hcloud ssh-key delete "$KEYNAME" >/dev/null 2>&1 || true
  # Drop a snapshot only if it failed boot-verification (SNAP_TO_CLEAN set);
  # a verified snapshot is the deliverable and must survive.
  [ -n "$SNAP_TO_CLEAN" ] && {
    echo "==> deleting unverified snapshot $SNAP_TO_CLEAN" >&2
    hcloud image delete "$SNAP_TO_CLEAN" >/dev/null 2>&1 || true
  }
  [ -n "$KEYDIR" ] && rm -rf "$KEYDIR"
}
trap cleanup EXIT

# ── Register an ephemeral SSH key for the build box ──────────────────────────
KEYDIR="$(mktemp -d)"
KEY="$KEYDIR/id"
KEYNAME="packer-ci-coordinator-$$"
KNOWN="$KEYDIR/known_hosts"
echo "==> registering ephemeral build SSH key"
ssh-keygen -t ed25519 -f "$KEY" -N "" -q
hcloud ssh-key create --name "$KEYNAME" --public-key-from-file "$KEY.pub" >/dev/null

# ── Create the build box from the stock cloud image (normal boot) ────────────
echo "==> creating temp $TYPE server $SERVER from $base_image"
hcloud server create --name "$SERVER" --type "$TYPE" --image "$base_image" --ssh-key "$KEYNAME" >/dev/null
NODE_IP=$(hcloud server ip "$SERVER")
echo "==> server up at $NODE_IP -- waiting for sshd (cloud-init authorizes our key)"
for _ in $(seq 1 60); do
  ssh_node true 2>/dev/null && break
  sleep 4
done
ssh_node true || {
  echo "build box ssh never came up at $NODE_IP" >&2
  exit 1
}

# ── Provision: dockerd + operator key, primed to re-init per clone ──
# Install docker.io so a freshly cloned coordinator can run job containers, bake
# the operator key (below) for debugging, and reset per-instance identity state
# so cloud-init re-runs cleanly and growpart expands root onto the larger
# fleeting server_type. fleeting injects its own ephemeral SSH key + user_data
# via cloud-init at clone time.
#
# No container images are baked in: detect pulls the CI image at runtime, cells
# reuse it through if-not-present, and gitlab-runner pulls its helper image from
# registry.gitlab.com. The reserved egress IP
# (terraform hcloud_primary_ip.ci_coordinator) reaches the registry reliably and
# the private CI image authenticates with the job's CI_JOB_TOKEN, so the bake
# stays a thin dockerd image carrying no registry credentials and nothing to
# refresh when ci_image or the gitlab-runner version changes.
echo "==> provisioning dockerd + operator key + resetting cloud-init seed state"
ssh_node "bash -seuo pipefail" <<'PROV'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# docker.io from the Ubuntu archive -- no third-party repo to pin or trust, and
# the coordinator only needs a working dockerd to run the CI-image container.
apt-get install -y -qq docker.io
systemctl enable docker >/dev/null
docker info >/dev/null

# Host-level debug/monitoring tooling for this ephemeral orchestrator. The real
# work runs inside the ci:latest container; these are for poking the HOST when a
# run misbehaves -- egress, routing, docker, resource pressure during the 60-wide
# fan-out. qemu-guest-agent adds hcloud console/password recovery + graceful ACPI
# shutdown (its udev rule auto-starts it once the virtio agent device appears, so
# no systemctl enable -- same as packer/scripts/chroot.sh). Keep this set in step
# with the fleet list in roles/user (ansible) and chroot.sh.
apt-get install -y -qq \
  qemu-guest-agent curl dnsutils mtr-tiny tcpdump htop jq iotop ncdu sysstat

# Authorize the operator's personal key on root so a cloned coordinator is
# reachable over its public IP for debugging (the ci-coordinator firewall admits
# SSH from the home WAN). fleeting injects its OWN ephemeral key via the Hetzner
# ssh_keys field at clone time; cloud-init's cc_ssh appends that to this file, so
# both keys coexist. Public key, not a secret -- rotate together with terraform
# local.operator_ssh_public_key and group_vars/all/main.yml ssh_public_keys.
install -d -m 0700 /root/.ssh
printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEFQDmZidqILmoI6o9f8KLz+0hJad+Xh4Lm5OLsYDZTa adrien.kohlbecker@gmail.com' >> /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys

# Shrink the snapshot and, more importantly, re-prime first-boot state so every
# fleeting clone re-runs cloud-init (key + user_data injection, growpart) with a
# fresh machine identity instead of inheriting this build box's.
apt-get clean
rm -rf /var/lib/apt/lists/*
cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
PROV

# ── Snapshot the powered-off disk ────────────────────────────────────────────
echo "==> powering off + snapshotting"
hcloud server poweroff "$SERVER" >/dev/null
imgid=$(hcloud server create-image "$SERVER" --type snapshot \
  --description "ci-coordinator-${ubuntu}-$(date '+%Y%m%d%H%M%S')" \
  --label "role=ci-coordinator" --label "ubuntu=${ubuntu}" |
  awk '/^Image/{print $2; exit}')
[ -n "$imgid" ] || {
  echo "could not determine the created snapshot image id" >&2
  exit 1
}
echo "==> snapshot image id=$imgid (available)"

# ── Prove it boots before tearing the build box down ─────────────────────────
# Rebuild the same temp box from the snapshot and wait for a live sshd. We may
# not authenticate (cloud-init re-provisions keys from the new server's
# user_data, which a bare rebuild lacks), but a live sshd proves the kernel
# booted, root mounted, and sshd started -- the exact surface a broken snapshot
# would fail. A bad snapshot left in place would become fleeting's image, so on
# failure we delete it (via SNAP_TO_CLEAN) and fail the run.
echo "==> verifying boot: rebuilding server from snapshot $imgid"
SNAP_TO_CLEAN="$imgid"
hcloud server rebuild "$SERVER" --image "$imgid" >/dev/null
hcloud server poweron "$SERVER" >/dev/null 2>&1 || true
sleep 40
waited=0
while [ "$waited" -lt 300 ]; do
  node_sshd_up "$NODE_IP" && {
    echo "==> boot verified: $NODE_IP reached a live sshd from the snapshot"
    SNAP_TO_CLEAN=""
    break
  }
  sleep 5
  waited=$((waited + 5))
done
[ -z "$SNAP_TO_CLEAN" ] || {
  echo "snapshot $imgid did not boot to a working sshd at $NODE_IP" >&2
  exit 1
}

# Keep the snapshot family bounded (newest 2 + any running server's image),
# same policy as the fox bake.
mise run packer:hcloud-prune-snapshots -- "role=ci-coordinator,ubuntu=$ubuntu"

echo "==> DONE. Coordinator snapshot $imgid labelled role=ci-coordinator,ubuntu=$ubuntu (boot-verified)."
echo "    fleeting selects it via image = \"\$id\" or the newest role=ci-coordinator label;"
echo "    wire it into roles/gitlab_runner (gitlab_runner_coordinator_snapshot)."

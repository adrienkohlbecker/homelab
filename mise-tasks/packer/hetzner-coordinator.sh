#!/usr/bin/env bash
#MISE description="Bake the CI coordinator's Hetzner snapshot: stock Ubuntu + dockerd + the CI and gitlab-runner-helper images baked in, the boot image the fleeting docker-autoscaler clones per pipeline (notes/ci_aws_test_cells.md). Both images are pulled through the AWS ECR pull-through cache (Hetzner -> registry.gitlab.com flaps; Hetzner -> ECR Frankfurt is reliable) and retagged to the gitlab refs the runner names, so the coordinator runs them from the local cache (pull_policy=if-not-present) without touching a registry at runtime. Rebake when ci_image, the gitlab-runner version, docker, or the base OS changes."
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"

ubuntu="${usage_ubuntu}"
case "$ubuntu" in
noble) base_image="ubuntu-24.04" ;;
resolute) base_image="ubuntu-26.04" ;;
*)
  echo "unsupported ubuntu: $ubuntu (expected noble or resolute)" >&2
  exit 1
  ;;
esac

API="https://api.hetzner.cloud/v1"
AUTH=(-H "Authorization: Bearer ${HCLOUD_TOKEN}")
# The temp build box is throwaway; size is irrelevant to the snapshot. cpx22's
# 80G disk is <= every fleeting target (cx53 is 320G), so a server cloned from
# this snapshot grows root via cloud-init growpart -- a larger base would cap
# which server_types could launch it.
TYPE="cpx22"
SERVER="packer-ci-coordinator"

# Minimal hcloud REST helpers -- deliberately self-contained rather than
# sourcing _hetzner_rescue.sh, whose lifecycle is rescue-mode + dd-stream
# specific (and drives the live fox bake): this flow is a normal boot +
# provision + snapshot. The api()/pyget() shape mirrors that lib on purpose.
api() { # METHOD PATH [JSON]
  if [ -n "${3:-}" ]; then
    curl -fsS -X "$1" "${AUTH[@]}" -H "Content-Type: application/json" -d "$3" "${API}$2"
  else
    curl -fsS -X "$1" "${AUTH[@]}" "${API}$2"
  fi
}
pyget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
sid() { api GET "/servers?name=$SERVER" | pyget 'd["servers"][0]["id"] if d["servers"] else ""'; }
sip() { api GET "/servers?name=$SERVER" | pyget 'd["servers"][0]["public_net"]["ipv4"]["ip"] if d["servers"] else ""'; }
sstatus() { api GET "/servers?name=$SERVER" | pyget 'd["servers"][0]["status"] if d["servers"] else ""'; }

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

KEYDIR="" KID="" SNAP_TO_CLEAN=""

cleanup() {
  local id
  id=$(sid || true)
  [ -n "$id" ] && {
    echo "==> deleting temp server $id"
    api DELETE "/servers/$id" >/dev/null 2>&1 || true
  }
  [ -n "$KID" ] && api DELETE "/ssh_keys/$KID" >/dev/null 2>&1 || true
  # Drop a snapshot only if it failed boot-verification (SNAP_TO_CLEAN set);
  # a verified snapshot is the deliverable and must survive.
  [ -n "$SNAP_TO_CLEAN" ] && {
    echo "==> deleting unverified snapshot $SNAP_TO_CLEAN" >&2
    api DELETE "/images/$SNAP_TO_CLEAN" >/dev/null 2>&1 || true
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
KID=$(api POST "/ssh_keys" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"public_key":open(sys.argv[2]).read().strip()}))' "$KEYNAME" "$KEY.pub")" | pyget 'd["ssh_key"]["id"]')

# ── Create the build box from the stock cloud image (normal boot) ────────────
echo "==> creating temp $TYPE server $SERVER from $base_image"
api POST "/servers" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"server_type":sys.argv[2],"image":sys.argv[3],"ssh_keys":[int(sys.argv[4])],"start_after_create":True}))' "$SERVER" "$TYPE" "$base_image" "$KID")" >/dev/null
for _ in $(seq 1 60); do
  [ "$(sstatus)" = "running" ] && break
  sleep 2
done
[ "$(sstatus)" = "running" ] || {
  echo "server never reached running (status: $(sstatus))" >&2
  exit 1
}
NODE_IP=$(sip)
echo "==> server up at $NODE_IP -- waiting for sshd (cloud-init authorizes our key)"
for _ in $(seq 1 60); do
  ssh_node true 2>/dev/null && break
  sleep 4
done
ssh_node true || {
  echo "build box ssh never came up at $NODE_IP" >&2
  exit 1
}

# ── Provision: dockerd + the CI image baked in, primed to re-init per clone ──
# Bake docker.io and pull the CI image so a freshly cloned coordinator already
# carries the layers locally. fleeting injects its own ephemeral SSH key +
# user_data via cloud-init at clone time; we additionally bake the operator key
# (below) and reset per-instance identity state so cloud-init re-runs cleanly and
# growpart expands root onto the larger fleeting server_type.
#
# The image is pulled THROUGH the AWS ECR pull-through cache
# (<acct>.dkr.ecr.eu-central-1.amazonaws.com/gitlab/...), not directly from
# registry.gitlab.com: Hetzner nbg1 -> registry.gitlab.com flaps at the
# connection level (intermittent i/o timeouts + anonymous-throttle 403s, and an
# authenticated docker login times out pre-auth all the same), while Hetzner ->
# ECR eu-central-1 (Frankfurt) is a short reliable hop. AWS holds the GitLab
# creds (terraform aws_ci.tf ci_ecr_gitlab) and does the registry.gitlab.com
# fetch itself. We authenticate to ECR with a short-lived token minted from the
# workstation's AWS creds, piped to the build box and wiped before the snapshot,
# so no credential is baked in. The image is retagged to the gitlab ref the
# runner config names (config.toml.j2), so the coordinator runs it from the local
# cache with pull_policy=if-not-present and never contacts a registry at runtime.
ECR_REGION="eu-central-1"
ECR_REG="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${ECR_REGION}.amazonaws.com"
aws ecr get-login-password --region "$ECR_REGION" | ssh_node "install -m 0600 /dev/stdin /root/.ecr_pw"

# The docker executor names its helper image at x86_64-v<runner version>. Lock
# the baked tag to the same pin the role installs (group_vars/all/versions.yml)
# so the helper image always matches the running gitlab-runner binary.
runner_version="$(python3 -c 'import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))["gitlab_runner_version"])' "$(dirname "$0")/../../group_vars/all/versions.yml")"

echo "==> provisioning dockerd + CI/helper images + operator key + resetting cloud-init seed state"
ssh_node "ECR_REG=$ECR_REG RUNNER_VER=$runner_version bash -seuo pipefail" <<'PROV'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# docker.io from the Ubuntu archive -- no third-party repo to pin or trust, and
# the coordinator only needs a working dockerd to run the CI-image container.
apt-get install -y -qq docker.io
systemctl enable docker >/dev/null
docker info >/dev/null

# Pull each image the coordinator runs at job time THROUGH the ECR pull-through
# cache, then retag it to the registry.gitlab.com ref the runner names so the
# runtime pull resolves from the local cache (pull_policy=if-not-present) without
# touching a registry. Two images: the project CI job image, and the
# gitlab-runner-helper the docker executor starts for git/artifacts/cache (same
# Hetzner -> registry.gitlab.com flap, so bake it too). We log out and delete
# both the token file and the docker config right after, before the snapshot, so
# no credential is baked in. The retry rides out ordinary network blips.
docker login "$ECR_REG" --username AWS --password-stdin </root/.ecr_pw

pull_retag() { # ECR_REF GITLAB_REF
  local ecr="$1" ref="$2" i
  for i in $(seq 1 5); do
    docker pull "$ecr" && break
    [ "$i" = 5 ] && {
      echo "image pull failed after 5 attempts: $ecr" >&2
      exit 1
    }
    echo "==> pull attempt $i failed for $ecr, retrying in 15s" >&2
    sleep 15
  done
  docker tag "$ecr" "$ref"
  docker rmi "$ecr" >/dev/null
}

pull_retag "$ECR_REG/gitlab/akohlbecker/homelab/ci:latest" \
  "registry.gitlab.com/akohlbecker/homelab/ci:latest"
pull_retag "$ECR_REG/gitlab/gitlab-org/gitlab-runner/gitlab-runner-helper:x86_64-v${RUNNER_VER}" \
  "registry.gitlab.com/gitlab-org/gitlab-runner/gitlab-runner-helper:x86_64-v${RUNNER_VER}"

docker logout "$ECR_REG" >/dev/null 2>&1 || true
rm -f /root/.ecr_pw /root/.docker/config.json

# No boot-time registry refresh: the coordinator has no registry it can reach at
# runtime (Hetzner -> registry.gitlab.com flaps, and pulling through ECR would
# need a standing AWS credential on this ephemeral box). The snapshot is the
# source of truth for both baked images -- rebake it
# (mise run packer:hetzner-coordinator) when ci_image or the gitlab-runner
# version changes. pull_policy=if-not-present (config.toml.j2) makes the baked
# images the correctness path, not just a warm cache.

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
api POST "/servers/$(sid)/actions/poweroff" '{}' >/dev/null || true
for _ in $(seq 1 30); do
  [ "$(sstatus)" = "off" ] && break
  sleep 2
done
[ "$(sstatus)" = "off" ] || {
  echo "server never powered off (status: $(sstatus))" >&2
  exit 1
}
imgid=$(api POST "/servers/$(sid)/actions/create_image" "$(python3 -c 'import json,sys;print(json.dumps({"type":"snapshot","description":"ci-coordinator-"+sys.argv[1]+"-"+sys.argv[2],"labels":{"role":"ci-coordinator","ubuntu":sys.argv[1]}}))' "$ubuntu" "$(date '+%Y%m%d%H%M%S')")" | pyget 'd["image"]["id"]')
echo "==> snapshot image id=$imgid (waiting for available)"
st=""
for _ in $(seq 1 120); do
  st=$(api GET "/images/$imgid" | pyget 'd["image"]["status"]')
  [ "$st" = "available" ] && break
  sleep 5
done
[ "$st" = "available" ] || {
  echo "snapshot $imgid never became available (status: $st)" >&2
  SNAP_TO_CLEAN="$imgid"
  exit 1
}

# ── Prove it boots before tearing the build box down ─────────────────────────
# Rebuild the same temp box from the snapshot and wait for a live sshd. We may
# not authenticate (cloud-init re-provisions keys from the new server's
# user_data, which a bare rebuild lacks), but a live sshd proves the kernel
# booted, root mounted, and sshd started -- the exact surface a broken snapshot
# would fail. A bad snapshot left in place would become fleeting's image, so on
# failure we delete it (via SNAP_TO_CLEAN) and fail the run.
echo "==> verifying boot: rebuilding server from snapshot $imgid"
SNAP_TO_CLEAN="$imgid"
api POST "/servers/$(sid)/actions/rebuild" "$(python3 -c 'import json,sys;print(json.dumps({"image":int(sys.argv[1])}))' "$imgid")" >/dev/null
api POST "/servers/$(sid)/actions/poweron" '{}' >/dev/null 2>&1 || true
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

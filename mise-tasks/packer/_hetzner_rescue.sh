#!/usr/bin/env bash
# Temp Hetzner rescue-server lifecycle, shared by the two image-publish
# paths:
#   - packer:hetzner       (mechanic 2) streams a pre-built raw image off
#                          the runner's filesystem onto /dev/sda.
#   - packer:hetzner-bake  (mechanic 1+2) bakes on an EC2 surrogate and has
#                          the build instance stream its disk straight here,
#                          so no 20G image ever lands on the runner.
#
# hcloud authenticates on its own: from $HCLOUD_TOKEN in CI (the job variable),
# or the local CLI context (~/.config/hcloud/cli.toml) on the workstation. The
# caller just sources this lib and runs: rescue_init -> `trap rescue_cleanup
# EXIT` -> rescue_create -> [write /dev/sda] -> rescue_snapshot "$UBUNTU".
#
# Exposes to the caller: RESCUE_IP, RESCUE_ID, the runner-side ssh_rescue()
# helper, and KEY (path to the ephemeral private key authorized on the
# rescue server -- packer:hetzner-bake hands KEY to the build instance so it
# can ssh here for the direct stream).
#
# Every hcloud verb here blocks on its server action (create/enable-rescue/
# reset/poweroff/create-image/rebuild/poweron all poll to completion), so the
# server reaches the requested state by the time the call returns -- no manual
# status-poll loops. The only waits left are for sshd, which is not an hcloud
# action.

TYPE="cpx22"
SERVER="packer-hetzner-upload"

# Canonical rescue-side receive pipeline, shared by both publish paths. The
# EC2 stream provisioner (packer/aws/ami.pkr.hcl) hardcodes the same string --
# keep them in sync. mbuffer absorbs network jitter so the dd write never
# stalls the TCP stream; zstd matches the rpool's own on-disk compression and
# beats gzip on both speed and ratio. conv=sparse skips zero blocks on write.
# 512M buffer stays well inside the cpx22 rescue's RAM-backed root (~3.7 GB).
# rescue_create installs zstd + mbuffer before this runs.
# shellcheck disable=SC2034  # consumed by the scripts that source this lib
RESCUE_RECV='mbuffer -q -m 512M | zstd -dc | dd of=/dev/sda bs=64M conv=sparse status=progress; sync'

# IdentitiesOnly=yes: offer only the ephemeral key, not every identity in a
# forwarded agent -- else the rescue sshd hits MaxAuthTries before the right
# one. Uses the RESCUE_IP/KEY/KNOWN globals rescue_init + rescue_create set.
ssh_rescue() { ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=5 "root@$RESCUE_IP" "$@"; }

# True when host:22 has a live sshd, whether or not it accepts our key. rc 0
# means our ephemeral key was authorized; a "permission denied"/auth-failure
# message means sshd answered but rejected us -- both prove the OS booted far
# enough to start sshd. A refused or timed-out connection means it has not (yet)
# booted. Cross-platform: leans on ssh's own ConnectTimeout, no nc/timeout
# binary needed (hetzner.sh can run on macOS, which ships neither).
rescue_sshd_up() { # IP
  local out
  out=$(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 -o PreferredAuthentications=publickey \
    "root@$1" true 2>&1) && return 0
  printf '%s' "$out" | grep -qiE 'permission denied|authentication failure|too many authentication'
}

rescue_init() {
  KEYDIR="$(mktemp -d)"
  KEY="$KEYDIR/id"
  KEYNAME="packer-hetzner-upload-$$"
  KNOWN="$KEYDIR/known_hosts"
  echo "==> registering ephemeral rescue SSH key"
  ssh-keygen -t ed25519 -f "$KEY" -N "" -q
  hcloud ssh-key create --name "$KEYNAME" --public-key-from-file "$KEY.pub" >/dev/null
}

rescue_cleanup() {
  echo "==> deleting temp server $SERVER"
  hcloud server delete "$SERVER" >/dev/null 2>&1 || true
  [ -n "${KEYNAME:-}" ] && hcloud ssh-key delete "$KEYNAME" >/dev/null 2>&1 || true
  rm -rf "$KEYDIR"
}

# Create the temp server, boot it into the rescue system, and block until
# its rescue sshd answers. Sets RESCUE_ID + RESCUE_IP.
rescue_create() {
  echo "==> creating temp $TYPE server $SERVER"
  hcloud server create --name "$SERVER" --type "$TYPE" --image ubuntu-22.04 --ssh-key "$KEYNAME" >/dev/null
  RESCUE_ID=$(hcloud server describe "$SERVER" -o format='{{.ID}}')
  RESCUE_IP=$(hcloud server ip "$SERVER")
  echo "==> server $RESCUE_ID up at $RESCUE_IP"

  echo "==> enabling rescue + hard reset"
  hcloud server enable-rescue "$SERVER" --type linux64 --ssh-key "$KEYNAME" >/dev/null
  hcloud server reset "$SERVER" >/dev/null
  sleep 40
  for _ in $(seq 1 40); do
    ssh_rescue true 2>/dev/null && break
    sleep 4
  done
  ssh_rescue true || {
    echo "rescue ssh never came up at $RESCUE_IP" >&2
    exit 1
  }
  # Confirm we're actually in rescue (overlay root), not the installed OS.
  ssh_rescue 'findmnt -no FSTYPE / | grep -q overlay' || {
    echo "server did not enter rescue" >&2
    exit 1
  }

  # Fast decompress (zstd) + a network buffer (mbuffer) for the stream; the
  # Debian rescue ships neither. apt here has internet + a working mirror.
  echo "==> installing zstd + mbuffer in the rescue"
  ssh_rescue 'apt-get update -qq && apt-get install -y -qq zstd mbuffer >/dev/null' || {
    echo "failed to install zstd/mbuffer in the rescue" >&2
    exit 1
  }
}

# Prove the freshly-created snapshot actually boots before we tear the temp
# server down: rebuild that same server from the snapshot (this wipes /dev/sda,
# whose contents we have already captured) and wait for a working sshd. We
# cannot authenticate -- the image's cloud-init provisions the operator's keys,
# not our ephemeral rescue key -- but a live sshd proves the kernel booted, the
# rpool imported, root mounted, and sshd started, which is exactly the failure
# surface a non-bootable bake would hit. A bad snapshot left in place would
# otherwise become terraform's newest-matching pick, so on failure we delete it
# and fail the run.
rescue_verify_boot() { # IMGID
  local imgid="$1" waited=0
  echo "==> verifying boot: rebuilding server $RESCUE_ID from snapshot $imgid"
  hcloud server rebuild "$SERVER" --image "$imgid" >/dev/null
  # rebuild keeps the prior power state (we powered off to snapshot); poweron
  # brings it back up. Harmless no-op if rebuild already booted it.
  hcloud server poweron "$SERVER" >/dev/null 2>&1 || true

  # Give it time to POST, import the rpool, mount root, and start sshd before
  # probing. Bounded poll -- a never-booting image surfaces as a quick failure,
  # never a silent hang.
  sleep 40
  while [ "$waited" -lt 300 ]; do
    rescue_sshd_up "$RESCUE_IP" && {
      echo "==> boot verified: $RESCUE_IP reached a live sshd from the snapshot"
      return 0
    }
    sleep 5
    waited=$((waited + 5))
  done

  echo "snapshot $imgid did not boot to a working sshd at $RESCUE_IP" >&2
  echo "    deleting the bad snapshot so terraform never selects it" >&2
  hcloud image delete "$imgid" >/dev/null 2>&1 || true
  exit 1
}

# Power off (so the image captures quiesced disks), snapshot /dev/sda, rebuild
# the temp server from it to confirm it boots, then prune the family to the
# newest 2.
rescue_snapshot() { # UBUNTU
  local ubuntu="$1" imgid
  echo "==> powering off + snapshotting"
  hcloud server poweroff "$SERVER" >/dev/null
  imgid=$(hcloud server create-image "$SERVER" --type snapshot \
    --description "ubuntu-zfs-${ubuntu}-$(date '+%Y%m%d%H%M%S')" \
    --label "os=ubuntu-zfs" --label "ubuntu=${ubuntu}" |
    awk '/^Image/{print $2; exit}')
  [ -n "$imgid" ] || {
    echo "could not determine the created snapshot image id" >&2
    exit 1
  }
  echo "==> snapshot image id=$imgid (available)"

  # Confirm the snapshot boots on the same temp server before it is torn down.
  # Fails the run (and deletes the bad snapshot) if it does not.
  rescue_verify_boot "$imgid"

  mise run packer:hcloud-prune-snapshots -- "os=ubuntu-zfs,ubuntu=$ubuntu"

  echo "==> DONE. Snapshot $imgid labelled os=ubuntu-zfs,ubuntu=$ubuntu (boot-verified)."
  echo "    Terraform's data.hcloud_image picks the newest matching snapshot automatically;"
  echo "    deploy by recreating a server (tofu taint/replace) — note that wipes the disk."
}

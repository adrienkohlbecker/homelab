#!/usr/bin/env bash
#MISE description="Upload a pre-built ZFS-root disk image to a Hetzner Cloud snapshot. Build the image first with `mise run packer:build hetzner` (qemu/KVM); this streams that raw image onto a throwaway Hetzner rescue server and snapshots it."
#USAGE arg "[image]" help="Path to the raw rpool disk image (default: the packer:build hetzner artifact for --ubuntu)"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu codename -- snapshot label + default image path" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

TYPE="cpx22"
SERVER="packer-hetzner-upload"
RESCUE_ORPHAN_MAX_AGE_HOURS=2

# zstd matches the rpool compression and mbuffer absorbs network jitter.
RESCUE_RECV='mbuffer -q -m 512M | zstd -dc | dd of=/dev/sda bs=64M conv=sparse status=progress; sync'

ssh_rescue() { ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=5 "root@$RESCUE_IP" "$@"; }

# The bulk stream ignores user SSH configuration and prefers hardware AES.
ssh_rescue_bulk() { ssh -F none -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=5 -o 'Ciphers=^aes128-gcm@openssh.com' "root@$RESCUE_IP" "$@"; }

wait_for_rescue_sshd() {
  sleep 40
  for _ in $(seq 1 40); do
    ssh_rescue true 2>/dev/null && return 0
    sleep 4
  done
  return 1
}

# Probe sshd without authenticating so cloud-init forced commands cannot delay
# the bounded boot poll.
rescue_sshd_up() { # IP
  local out
  out=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    -o PreferredAuthentications=none "root@$1" true 2>&1) && return 0
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

# A killed job can strand the static server name. Reap only old instances so
# concurrent runs collide safely instead of deleting each other.
rescue_reap_orphan() {
  local decision
  decision=$(hcloud server list -o json | python3 -c '
import datetime, json, sys
name, max_age_h = sys.argv[1], float(sys.argv[2])
match = next((s for s in json.load(sys.stdin) if s["name"] == name), None)
if match is None:
    print("absent")
else:
    created = datetime.datetime.fromisoformat(match["created"].replace("Z", "+00:00"))
    age_h = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600
    print(f"reap {age_h:.1f}" if age_h >= max_age_h else f"young {age_h:.1f}")
' "$SERVER" "$RESCUE_ORPHAN_MAX_AGE_HOURS" 2>/dev/null) || return 0
  case "$decision" in
  reap*)
    echo "==> reaping stranded $SERVER (${decision#reap }h old, prior killed run) before re-creating"
    hcloud server delete "$SERVER" >/dev/null 2>&1 || true
    ;;
  young*)
    echo "==> $SERVER exists and is ${decision#young }h old (< ${RESCUE_ORPHAN_MAX_AGE_HOURS}h); leaving it -- the create below fails loudly if it is a real collision" >&2
    ;;
  esac
}

rescue_create() {
  rescue_reap_orphan
  echo "==> creating temp $TYPE server $SERVER"
  hcloud server create --name "$SERVER" --type "$TYPE" --image ubuntu-24.04 --ssh-key "$KEYNAME" >/dev/null
  RESCUE_ID=$(hcloud server describe "$SERVER" -o format='{{.ID}}')
  RESCUE_IP=$(hcloud server ip "$SERVER")
  echo "==> server $RESCUE_ID up at $RESCUE_IP"

  echo "==> enabling rescue + hard reset"
  hcloud server enable-rescue "$SERVER" --type linux64 --ssh-key "$KEYNAME" >/dev/null
  hcloud server reset "$SERVER" >/dev/null
  wait_for_rescue_sshd || {
    echo "rescue ssh never came up at $RESCUE_IP" >&2
    exit 1
  }
  ssh_rescue 'findmnt -no FSTYPE / | grep -q overlay' || {
    echo "server did not enter rescue" >&2
    exit 1
  }

  echo "==> installing zstd + mbuffer in the rescue"
  ssh_rescue 'apt-get update -qq && apt-get install -y -qq zstd mbuffer >/dev/null' || {
    echo "failed to install zstd/mbuffer in the rescue" >&2
    exit 1
  }
}

# Rebuild the temporary server from the new snapshot and require a live sshd.
rescue_verify_boot() { # IMGID
  local imgid="$1" waited=0
  echo "==> verifying boot: rebuilding server $RESCUE_ID from snapshot $imgid"
  hcloud server rebuild "$SERVER" --image "$imgid" >/dev/null
  hcloud server poweron "$SERVER" >/dev/null 2>&1 || true

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
  rescue_collect_diagnostics
  echo "    deleting the bad snapshot so terraform never selects it" >&2
  hcloud image delete "$imgid" >/dev/null 2>&1 || true
  exit 1
}

# Best-effort post-mortem for a snapshot that failed boot verification.
rescue_collect_diagnostics() {
  echo "==> collecting boot-verify diagnostics (rebooting $SERVER into rescue)" >&2
  hcloud server enable-rescue "$SERVER" --type linux64 --ssh-key "$KEYNAME" >/dev/null 2>&1 || true
  hcloud server reset "$SERVER" >/dev/null 2>&1 || true
  wait_for_rescue_sshd || {
    echo "    rescue did not come back up -- no diagnostics collected" >&2
    return 0
  }

  echo "    --- /dev/sda partition table + GPT integrity ---" >&2
  ssh_rescue 'lsblk -o NAME,SIZE,FSTYPE,PARTLABEL /dev/sda; echo; sgdisk -p /dev/sda; echo; sgdisk -v /dev/sda' >&2 2>&1 || true

  echo "    --- ESP (/dev/sda2) bootloader tree ---" >&2
  # shellcheck disable=SC2016  # $m expands on the rescue host, not locally
  ssh_rescue 'm=$(mktemp -d); mount -o ro /dev/sda2 "$m" && { find "$m/EFI" -maxdepth 2 | sort; echo; cat "$m/EFI/refind/refind.conf" 2>/dev/null; umount "$m"; }' >&2 2>&1 || echo "    could not read the ESP" >&2

  echo "    --- rpool last-boot journal + cloud-init (needs zfs; skipped if absent) ---" >&2
  # shellcheck disable=SC2016  # remote variables expand on the rescue host
  ssh_rescue '
    modprobe zfs 2>/dev/null || { echo "(no zfs module in this rescue; rpool journal unavailable)"; exit 0; }
    zpool import -fN -o readonly=on -R /mnt rpool || { echo "(rpool import failed)"; exit 0; }
    ds=$(zfs list -H -o name | grep -m1 "/ROOT/" || true)
    [ -n "$ds" ] && zfs mount -o ro "$ds" 2>/dev/null || true
    echo "--- journalctl -b -1 (last 200) ---"; journalctl -D /mnt/var/log/journal -b -1 --no-pager 2>/dev/null | tail -n 200 || echo "(no journal)"
    echo "--- cloud-init-output.log (last 100) ---"; tail -n 100 /mnt/var/log/cloud-init-output.log 2>/dev/null || echo "(none)"
  ' >&2 2>&1 || true
}

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

  rescue_verify_boot "$imgid"
  mise run packer:hcloud-prune-snapshots -- "os=ubuntu-zfs,ubuntu=$ubuntu"

  echo "==> DONE. Snapshot $imgid labelled os=ubuntu-zfs,ubuntu=$ubuntu (boot-verified)."
  echo "    Terraform's data.hcloud_image picks the newest matching snapshot automatically;"
  echo "    deploy by recreating a server (tofu taint/replace) — note that wipes the disk."
}

main() {
  UBUNTU="$usage_ubuntu"
  # Default to the artifact `mise run packer:build hetzner` publishes (raw, on
  # lab). The upload streams the image straight onto /dev/sda, so it must be a
  # raw disk image, not a qcow2 -- pass an explicit path if it lives elsewhere.
  IMG="${usage_image:-${HOMELAB_CI_DIR}/${UBUNTU}/hetzner/packer-ubuntu-1.raw}"
  [ -f "$IMG" ] || {
    echo "no disk image at $IMG -- build it first: mise run packer:build hetzner" >&2
    exit 1
  }

  rescue_init
  trap rescue_cleanup EXIT
  rescue_create

  # The rescue server was created from a stock ubuntu-24.04 image, so /dev/sda
  # already carries that image's GPT (backup header at the true ~76G disk end)
  # and its filesystems. We stream a smaller raw image onto the front with
  # conv=sparse, which never touches the tail -- leaving a stale backup GPT and
  # stale partitions past the image end that disagree with its own primary
  # GPT and can block the firmware from booting the snapshot. Discard the whole
  # device first so only the streamed image's structures survive; the in-rescue
  # install path (provision.sh) wipes equivalently before partitioning. Fall back
  # to wipefs if the device rejects discard -- the post-stream sgdisk -e below
  # rewrites the backup header regardless, so a discard failure is non-fatal.
  echo "==> wiping /dev/sda before streaming (clears the rescue image's stale GPT + tail)"
  ssh_rescue 'blkdiscard -f /dev/sda || wipefs -a /dev/sda'

  # Stream the raw image onto /dev/sda via the shared rescue receive pipeline.
  # Compress with zstd here; the rpool blocks are already compressed, so speed
  # beats ratio. mbuffer on the send side, when present, smooths the ssh handoff.
  echo "==> streaming $IMG ($(du -h "$IMG" | cut -f1)) onto /dev/sda (this takes a few minutes)"
  if command -v mbuffer >/dev/null; then
    zstd -1 -T0 -c "$IMG" | mbuffer -m 512M | ssh_rescue_bulk "$RESCUE_RECV"
  else
    zstd -1 -T0 -c "$IMG" | ssh_rescue_bulk "$RESCUE_RECV"
  fi

  # The streamed image's GPT backup header sits at the image's own end, not the
  # true ~76G disk end the firmware expects. Relocate it so the GPT
  # is consistent with the real disk and the firmware boots the snapshot cleanly.
  # hetzner_growpart.service relocates it too, but only after a successful boot --
  # fixing it here keeps a misplaced backup header from blocking that boot.
  echo "==> relocating the GPT backup header to the disk end"
  ssh_rescue 'sgdisk -e /dev/sda'

  # Read the streamed GPT directly rather than relying on the rescue kernel's
  # stale partition nodes, then refuse to publish an image that lost the
  # rebuild-only Podman partition. The boot verifier below proves the OS comes
  # up; this check proves the raw device the Podman role will format on first
  # converge has the required label and size.
  echo "==> verifying the dedicated Podman partition"
  ssh_rescue 'sgdisk -i 4 /dev/sda | grep -Eq "^Partition name: .podman.$" && sgdisk -i 4 /dev/sda | grep -Fq "40.0 GiB"'

  rescue_snapshot "$UBUNTU"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi

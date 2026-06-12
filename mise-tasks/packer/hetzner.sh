#!/usr/bin/env bash
#MISE description="Upload a pre-built ZFS-root disk image to a Hetzner Cloud snapshot (mechanic 2). Build the image first with `mise run packer:hetzner-bake` (EC2) or `mise run packer:build hetzner` (qemu/KVM)."
#USAGE arg "[image]" help="Path to the rpool disk image, raw or raw.gz (default: the packer:build hetzner artifact for --ubuntu)"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu codename -- snapshot label + default image path" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"

UBUNTU="$usage_ubuntu"
# Default to the artifact `mise run packer:build hetzner` publishes (raw, on
# lab). The upload streams the image straight onto /dev/sda, so it must be a
# raw disk image, not a qcow2 -- pass an explicit path if it lives elsewhere.
IMG="${usage_image:-${HOMELAB_CI_DIR}/${UBUNTU}/hetzner/packer-ubuntu-1.raw}"
[ -f "$IMG" ] || {
  echo "no disk image at $IMG -- build it first: mise run packer:build hetzner" >&2
  exit 1
}

API="https://api.hetzner.cloud/v1"
AUTH=(-H "Authorization: Bearer ${HCLOUD_TOKEN}")
TYPE="cpx22"
SERVER="packer-hetzner-upload"

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

KEYDIR="$(mktemp -d)"
KEY="$KEYDIR/id"
KEYNAME="packer-hetzner-upload-$$"
KNOWN="$KEYDIR/known_hosts"
cleanup() {
  local id kid
  id=$(sid || true)
  [ -n "$id" ] && {
    echo "==> deleting temp server $id"
    api DELETE "/servers/$id" >/dev/null || true
  }
  kid=$(api GET "/ssh_keys?name=$KEYNAME" | pyget 'd["ssh_keys"][0]["id"] if d["ssh_keys"] else ""' || true)
  [ -n "$kid" ] && api DELETE "/ssh_keys/$kid" >/dev/null || true
  rm -rf "$KEYDIR"
}
trap cleanup EXIT

# --- phase 1: temp server + rescue (ephemeral key, self-contained) ---
echo "==> registering ephemeral rescue SSH key"
ssh-keygen -t ed25519 -f "$KEY" -N "" -q
kid=$(api POST "/ssh_keys" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"public_key":open(sys.argv[2]).read().strip()}))' "$KEYNAME" "$KEY.pub")" | pyget 'd["ssh_key"]["id"]')

echo "==> creating temp $TYPE server $SERVER"
api POST "/servers" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"server_type":sys.argv[2],"image":"ubuntu-22.04","ssh_keys":[int(sys.argv[3])],"start_after_create":True}))' "$SERVER" "$TYPE" "$kid")" >/dev/null
for _ in $(seq 1 60); do
  [ "$(sstatus)" = "running" ] && break
  sleep 2
done
id=$(sid)
IP=$(sip)

echo "==> enabling rescue + hard reset"
api POST "/servers/$id/actions/enable_rescue" "{\"type\":\"linux64\",\"ssh_keys\":[$kid]}" >/dev/null
api POST "/servers/$id/actions/reset" '{}' >/dev/null
sleep 40
# IdentitiesOnly=yes: offer only the ephemeral key, not every identity in a
# forwarded agent -- else the rescue sshd hits MaxAuthTries before the right one.
ssh_rescue() { ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=5 "root@$IP" "$@"; }
for _ in $(seq 1 40); do
  ssh_rescue true 2>/dev/null && break
  sleep 4
done
# Confirm we're actually in rescue (overlay root), not the installed OS.
ssh_rescue 'findmnt -no FSTYPE / | grep -q overlay' || {
  echo "server did not enter rescue" >&2
  exit 1
}

# --- phase 2: stream the image onto /dev/sda ---
# gzip (universally present in the Debian rescue) keeps the mostly-empty 20G
# image small on the wire; conv=sparse skips zero blocks on write. A .raw.gz
# (the packer:hetzner-bake artifact) is already wire-ready and streams as-is.
echo "==> streaming the Hetzner image onto /dev/sda (this takes a few minutes)"
case "$IMG" in
*.gz) cat "$IMG" ;;
*) gzip -c "$IMG" ;;
esac | ssh_rescue 'gzip -d | dd of=/dev/sda bs=64M conv=sparse status=progress; sync'

# --- phase 3: snapshot ---
echo "==> powering off + snapshotting"
api POST "/servers/$id/actions/poweroff" '{}' >/dev/null || true
for _ in $(seq 1 30); do
  [ "$(sstatus)" = "off" ] && break
  sleep 2
done
imgid=$(api POST "/servers/$id/actions/create_image" "$(python3 -c 'import json,sys;print(json.dumps({"type":"snapshot","description":"ubuntu-zfs-"+sys.argv[1]+"-"+sys.argv[2],"labels":{"os":"ubuntu-zfs","ubuntu":sys.argv[1]}}))' "$UBUNTU" "$(date '+%Y%m%d%H%M%S')")" | pyget 'd["image"]["id"]')
echo "==> snapshot image id=$imgid (waiting for available)"
for _ in $(seq 1 120); do
  st=$(api GET "/images/$imgid" | pyget 'd["image"]["status"]')
  [ "$st" = "available" ] && break
  sleep 5
done

mise run packer:hcloud-prune-snapshots -- "os=ubuntu-zfs,ubuntu=$UBUNTU"

echo "==> DONE. Snapshot $imgid labelled os=ubuntu-zfs,ubuntu=$UBUNTU."
echo "    Terraform's data.hcloud_image picks the newest matching snapshot automatically;"
echo "    deploy by recreating a server (tofu taint/replace) — note that wipes the disk."
echo "    Validate first: create a throwaway cpx22 from it and confirm cloud-init creates ak."

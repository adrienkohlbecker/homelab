#!/usr/bin/env bash
#MISE description="Build the ZFS-root Ubuntu image and publish it as a Hetzner Cloud snapshot (mechanic 2). Run on lab — needs x86 KVM."
set -euo pipefail

# HCLOUD_TOKEN arrives as the literal op:// ref (file-based mise tasks don't
# resolve op://, per CLAUDE.md). Re-exec once under `op run --` so the API gets
# the real token; the guard env var prevents an infinite loop.
if [ -z "${HETZNER_OP_REEXEC:-}" ]; then
  exec env HETZNER_OP_REEXEC=1 op run -- "$0" "$@"
fi

# This build reuses qemu.pkr.hcl's shared source, whose arch_cfg is host-derived.
# On a Mac that resolves to aarch64+hvf and would silently build a useless arm
# image; the Hetzner image is x86_64-only. Fail loud rather than ship wrong arch.
if [ "$(uname -m)" != "x86_64" ]; then
  echo "the Hetzner image must build on x86_64 (KVM) — run on lab, not $(uname -m)." >&2
  exit 1
fi

UBUNTU="${1:-jammy}"
API="https://api.hetzner.cloud/v1"
AUTH=(-H "Authorization: Bearer ${HCLOUD_TOKEN}")
TYPE="cpx22"
SERVER="packer-hetzner-upload"
HERE="${MISE_CONFIG_ROOT}/packer"

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

OUT="$(mktemp -d)"
BUILD="$(mktemp -d)"
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
  rm -rf "$OUT" "$BUILD" "$KEYDIR"
}
trap cleanup EXIT

# --- phase 1: build the rpool disk image (qemu, on lab) ---
echo "==> building the Hetzner image via packer (qemu/KVM)"
packer init "$HERE"
packer build -only=qemu.hetzner \
  -var "ubuntu_name=$UBUNTU" \
  -var "upstream_mirrors=true" \
  -var "build_directory=$BUILD" \
  -var "output_directory=$OUT" \
  "$HERE"
# The build reuses the shared qemu.ubuntu source, so the rpool disk keeps that
# source's name (packer-ubuntu-1) + the extension post-processor's .raw suffix.
IMG="$OUT/hetzner/packer-ubuntu-1.raw"
[ -f "$IMG" ] || {
  echo "build produced no $IMG" >&2
  exit 1
}

# --- phase 2: temp server + rescue (ephemeral key, self-contained) ---
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

# --- phase 3: stream the image onto /dev/sda ---
# gzip (universally present in the Debian rescue) keeps the mostly-empty 20G
# image small on the wire; conv=sparse skips zero blocks on write.
echo "==> streaming the Hetzner image onto /dev/sda (this takes a few minutes)"
gzip -c "$IMG" | ssh_rescue 'gzip -d | dd of=/dev/sda bs=64M conv=sparse status=progress; sync'

# --- phase 4: snapshot ---
echo "==> powering off + snapshotting"
api POST "/servers/$id/actions/poweroff" '{}' >/dev/null || true
for _ in $(seq 1 30); do
  [ "$(sstatus)" = "off" ] && break
  sleep 2
done
imgid=$(api POST "/servers/$id/actions/create_image" "$(python3 -c 'import json,sys;print(json.dumps({"type":"snapshot","description":"ubuntu-zfs-"+sys.argv[1],"labels":{"os":"ubuntu-zfs","ubuntu":sys.argv[1]}}))' "$UBUNTU")" | pyget 'd["image"]["id"]')
echo "==> snapshot image id=$imgid (waiting for available)"
for _ in $(seq 1 120); do
  st=$(api GET "/images/$imgid" | pyget 'd["image"]["status"]')
  [ "$st" = "available" ] && break
  sleep 5
done

echo "==> DONE. Snapshot $imgid labelled os=ubuntu-zfs,ubuntu=$UBUNTU."
echo "    Terraform's data.hcloud_image picks the newest matching snapshot automatically;"
echo "    deploy by recreating a server (tofu taint/replace) — note that wipes the disk."
echo "    Validate first: create a throwaway cpx22 from it and confirm cloud-init creates ak."

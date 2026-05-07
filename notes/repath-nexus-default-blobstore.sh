#!/usr/bin/env bash
# repath-nexus-default-blobstore.sh — move the Nexus default blob store
# from /mnt/services/nexus/data/blobs/default to /mnt/scratch/nexus without
# telling Nexus. The container path /nexus-data/blobs/default stays the
# same — we just bind-mount the relocated directory at it. Nexus's DB
# never learns the underlying physical location moved, so blob references
# stay valid.
#
# Sequencing — apply these code changes BEFORE the next ansible run, or
# ansible will overwrite the unit and Nexus will come up with an empty
# blob store on the old (now-missing) path:
#
#   1. roles/nexus/templates/nexus.service.j2: replace
#        --volume /mnt/scratch/nexus:/nexus-data-scratch
#      with
#        --volume /mnt/scratch/nexus:/nexus-data/blobs/default
#
#   2. terraform/nexus.tf: drop the nexus_blobstore_file.scratch resource,
#      then  `mise run tf apply`  (it will destroy the empty scratch store
#      that was registered with Nexus). Or `tofu state rm` if you don't
#      want tofu to issue the delete.
#
# Run this script on lab as root. Reverse path lives at ${UNIT}.bak in
# case start fails — restore it, mv the data back, daemon-reload.
set -euo pipefail

SRC=/mnt/services/nexus/data/blobs/default
DST=/mnt/scratch/nexus
UNIT=/etc/systemd/system/nexus.service
HEALTH=http://localhost:8082/service/rest/v1/status/writable

[[ $EUID -eq 0 ]]  || { echo "run as root" >&2; exit 1; }
[[ -d "$SRC" ]]    || { echo "$SRC missing — already migrated?" >&2; exit 1; }
[[ -d "$DST" ]]    || { echo "$DST missing — expected the empty scratch blobstore dir" >&2; exit 1; }
[[ -f "$UNIT" ]]   || { echo "$UNIT not found" >&2; exit 1; }
grep -q '/mnt/scratch/nexus:/nexus-data-scratch' "$UNIT" \
  || { echo "expected /mnt/scratch/nexus:/nexus-data-scratch mount not in $UNIT" >&2; exit 1; }

# ----------------------------------------------------------------- 1. stop
echo "stopping nexus.service…"
systemctl stop nexus.service
sleep 2
systemctl is-active --quiet nexus.service \
  && { echo "nexus still active after stop" >&2; exit 1; }

# ---------------------------------------- 2. drop the empty scratch blobstore
# /mnt/scratch/nexus was provisioned for a separate scratch blob store that
# we're no longer using. Wipe it so the mv in step 3 lands the relocated
# default store at exactly that path.
echo "removing existing $DST contents…"
rm -rf "$DST"

# ------------------------------------------------------ 3. move data on disk
# /mnt/services and /mnt/scratch are different ZFS datasets, so this is a
# copy + unlink under the hood. For a multi-GB blob store this can take a
# while. mv handles cross-fs and preserves attrs; if interrupted, restart
# from scratch (the partial $DST will look empty-but-not-the-blobstore and
# trip nothing — re-running will fail because $SRC is gone, so manually
# resume with rsync if you have to).
echo "moving $SRC → $DST  (cross-fs copy; this can take a while)…"
mv "$SRC" "$DST"
chown -R nexus:nexus "$DST"

# --------------------------------------------------- 4. patch the unit file
# The role template should be updated in git too — see header. Editing the
# unit on disk lets us start before the next ansible run.
cp -a "$UNIT" "${UNIT}.bak"
sed -i 's|/mnt/scratch/nexus:/nexus-data-scratch|/mnt/scratch/nexus:/nexus-data/blobs/default|' "$UNIT"
grep -q '/mnt/scratch/nexus:/nexus-data/blobs/default' "$UNIT" \
  || { echo "sed did not patch $UNIT; restoring backup" >&2; mv "${UNIT}.bak" "$UNIT"; exit 1; }

systemctl daemon-reload

# ------------------------------------------------------ 5. start and verify
echo "starting nexus.service…"
systemctl start nexus.service

echo "waiting up to 5 min for /service/rest/v1/status/writable…"
for _ in $(seq 1 60); do
  if curl -fsS "$HEALTH" >/dev/null 2>&1; then
    echo "nexus is healthy."
    echo
    echo "verify a fetch through the proxy (e.g. an apt repo) before"
    echo "deleting ${UNIT}.bak. Once happy, push the role template +"
    echo "terraform changes from the script header so the next ansible"
    echo "run doesn't revert the unit."
    exit 0
  fi
  sleep 5
done

echo "nexus did not become healthy in 5 min" >&2
echo "  systemctl status nexus.service"
echo "  journalctl -u nexus.service --since '5 minutes ago'"
echo "rollback: mv $DST $SRC ; mv ${UNIT}.bak $UNIT ; systemctl daemon-reload ; systemctl start nexus" >&2
exit 1

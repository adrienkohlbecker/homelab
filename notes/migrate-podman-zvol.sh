#!/usr/bin/env bash
# migrate-podman-zvol.sh — move rpool/podman from /var/lib/containers/storage
# up one level to /var/lib/containers, and configure rootless storage on it.
set -euo pipefail

ZVOL=/dev/zvol/rpool/podman
OLD_MOUNT=/var/lib/containers/storage
NEW_MOUNT=/var/lib/containers
SERVICES_LIST=/tmp/podman-services.txt

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -b $ZVOL ]]   || { echo "$ZVOL not present" >&2; exit 1; }

pause() { read -rp ">>> $1 — press enter to continue (ctrl-c to abort) "; }

# ---------------------------------------------------------------- 1. preflight
# Query each candidate target explicitly: podman's overlay graph driver
# bind-mounts /var/lib/containers/storage/overlay onto itself for metacopy,
# so a plain `findmnt -o TARGET <source>` returns multiple lines.
if findmnt --source "$ZVOL" --target "$OLD_MOUNT" >/dev/null 2>&1; then
  echo "current mount: $OLD_MOUNT — proceeding"
elif findmnt --source "$ZVOL" --target "$NEW_MOUNT" >/dev/null 2>&1; then
  echo "already at $NEW_MOUNT, nothing to do"; exit 0
elif ! findmnt --source "$ZVOL" >/dev/null 2>&1; then
  echo "$ZVOL not mounted — aborting" >&2; exit 1
else
  echo "unexpected mount layout for $ZVOL:" >&2
  findmnt --source "$ZVOL" >&2
  exit 1
fi

echo "==> /etc/fstab line for the zvol:"
grep -E "^\s*${ZVOL//\//\\/}\s" /etc/fstab || { echo "  (none — bail)"; exit 1; }

# ----------------------------------------------------- 2. snapshot + stop svcs
echo "==> snapshotting active services that exec 'podman run' to $SERVICES_LIST"
# `--state=active` catches running, exited, and failed-but-loaded units so we
# also re-enable services that were down at script start. Use `if` (not `&&`)
# inside the while so the loop body always exits 0 — otherwise the last
# non-matching unit makes pipefail kill the script.
: > "$SERVICES_LIST"
systemctl list-units --type=service --state=active --plain --no-legend \
  | awk '{print $1}' \
  | while read -r u; do
      if systemctl show -p ExecStart --value "$u" | grep -q '/usr/bin/podman run'; then
        echo "$u" >> "$SERVICES_LIST"
      fi
    done
if [[ -s "$SERVICES_LIST" ]]; then
  cat "$SERVICES_LIST"
else
  echo "  (none found — script will skip stop/start of services)"
fi

pause "stop these services + podman.socket?"
xargs -r systemctl stop < "$SERVICES_LIST"
systemctl stop podman.socket 2>/dev/null || true

# verify nothing is still holding the mount
if fuser -m "$OLD_MOUNT" 2>/dev/null; then
  echo "processes still using $OLD_MOUNT (above) — investigate before continuing" >&2
  exit 1
fi

# ----------------------------------------------------------- 3. migrate layout
pause "unmount $OLD_MOUNT and shuffle zvol contents into a storage/ subdir?"
# -R brings down podman's overlay self-bind under storage/overlay along
# with the main ext4 mount.
umount -R "$OLD_MOUNT"

tmp=$(mktemp -d)
mount "$ZVOL" "$tmp"
echo "==> contents at zvol root before move:"; ls -la "$tmp"

( cd "$tmp"
  mkdir -p storage
  shopt -s dotglob nullglob
  for f in *; do
    [[ "$f" == "storage" || "$f" == "lost+found" ]] && continue
    mv -- "$f" storage/
  done )

echo "==> contents at zvol root after move:"; ls -la "$tmp"
echo "==> contents of new storage/ subdir:";  ls -la "$tmp/storage" | head -20

umount "$tmp"
rmdir "$tmp"

# ----------------------------------------------------------------- 4. fstab
pause "rewrite /etc/fstab so $ZVOL mounts at $NEW_MOUNT?"
cp /etc/fstab /etc/fstab.pre-podman-zvol-migrate
sed -i -E "s|^(\s*${ZVOL//\//\\/}\s+)${OLD_MOUNT//\//\\/}(\s+)|\1${NEW_MOUNT}\2|" /etc/fstab
echo "==> new fstab line:"
grep -E "^\s*${ZVOL//\//\\/}\s" /etc/fstab

# ----------------------------------------------------------------- 5. remount
pause "mount the zvol at $NEW_MOUNT?"
systemctl daemon-reload  # pick up updated fstab
mount "$NEW_MOUNT"
findmnt "$NEW_MOUNT"

# --------------------------------------------------------------- 6. rootless
# Drop-ins under storage.conf.d/ aren't loaded in rootless mode on podman 4.9,
# so we write /etc/containers/storage.conf directly. Set driver explicitly to
# silence the auto-pick warning.
pause "create /var/lib/containers/rootless (1777) and write storage.conf?"
install -d -m 1777 -o root -g root /var/lib/containers/rootless
install -d -m 0755 -o root -g root /etc/containers
cat >/etc/containers/storage.conf <<'EOF'
[storage]
driver = "overlay"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
rootless_storage_path = "/var/lib/containers/rootless/$USER"
EOF
rm -f /etc/containers/storage.conf.d/rootless-zvol.conf
ls -la /var/lib/containers/ /etc/containers/

# ------------------------------------------------------------ 7. start back up
pause "start podman.socket and the services we stopped?"
systemctl start podman.socket
xargs -r systemctl start < "$SERVICES_LIST"

echo
echo "==> done. quick checks:"
echo "  findmnt $NEW_MOUNT"
echo "  ls /var/lib/containers/storage   # should show overlay/, libpod/, etc."
echo "  podman info | grep -i graphroot   # /var/lib/containers/storage"
echo "  sudo -u <someuser> podman info | grep -i graphroot   # /var/lib/containers/rootless/<someuser>"

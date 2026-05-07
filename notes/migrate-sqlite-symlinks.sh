#!/usr/bin/env bash
# Migrate SQLite DBs from rpool/services (recordsize=128K) onto
# rpool/services/sqlite (recordsize=4K) for the ten services whose DB
# paths are hardcoded next to other state — the symlink-flavour batch.
#
# Run as root (sudo) on the homelab host. Idempotent for already-migrated
# DBs: existing symlinks at the source path are skipped.
#
# Phases:
#   1. stop services
#   2. mkdir per-service /mnt/services/sqlite/<svc> (chown to service user)
#   3. mv each DB (+ any -wal/-shm sidecars) onto the 4K dataset; mv
#      between datasets is copy+unlink, so the destination is rewritten
#      at the new recordsize
#   4. ansible-playbook over the affected roles — creates the symlinks,
#      updates the unit files, restarts the services
#   5. systemctl start as a safety net (no-op if ansible already started
#      them)
#
# Pre-existing migrations (pihole, homeassistant recorder DB, bazarr,
# overseerr, filebrowser, csplogger, mosquitto, healthchecks, gitea) are
# untouched — they used the directory-bind variant and were migrated in
# earlier batches.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

PLAYBOOK_DIR="${PLAYBOOK_DIR:-/home/ak/homelab}"
INVENTORY="${INVENTORY:-inventory.py}"
HOST="${HOST:-lab}"

# svc:owner:group:src_dir:db1[,db2,...]
# Paperless uses 0700 mode on its data dir; sqlite/paperless is created
# with the same mode by the install -m 0700 in phase 2 (override below).
SERVICES=(
  # "sonarr:sonarr:media:/mnt/services/sonarr:sonarr.db,logs.db"
  # "radarr:radarr:media:/mnt/services/radarr:radarr.db,logs.db"
  # "sabnzbd:sabnzbd:media:/mnt/services/sabnzbd/admin:history1.db"
  # "headphones:headphones:media:/mnt/services/headphones:headphones.db"
  # "profilarr:profilarr:profilarr:/mnt/services/profilarr:profilarr.db"
  # "jellyfin:jellyfin:media:/mnt/services/jellyfin/data:library.db,jellyfin.db"
  # "kuma:kuma:kuma:/mnt/services/kuma:kuma.db"
  # z2m intentionally excluded: its database.db is NDJSON, not SQLite, and
  # z2m rewrites it via tmpfile + rename — atomic-rename clobbers the
  # symlink, so the migrated copy gets orphaned on the first save.
  # "tautulli:tautulli:tautulli:/mnt/services/tautulli:tautulli.db"
  # "homeassistant:homeassistant:homeassistant:/mnt/services/homeassistant:zigbee.db"
  "paperless:paperless:paperless:/mnt/services/paperless/data:db.sqlite3,celerybeat-schedule.db"
)

# Jellyfin's introskipper plugin DB lives in a subdir; handled out-of-band
# below because it's optional and uses a different src_dir.
# Plex uses a directory bind, not a symlink — mv'd in bulk below because
# the Databases/ subdir contains both live DBs and dated full-DB backups.

ROLE_TAGS="paperless,plex"

confirm() {
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
}

# --- recovery trap ------------------------------------------------------
# If we exit before SUCCESS=true, attempt to start everything we stopped.
SUCCESS=false
STOPPED_SERVICES=()

cleanup_on_exit() {
  set +e
  $SUCCESS && return 0
  echo
  echo "==> trap: attempting to restart stopped services"
  for ((i=${#STOPPED_SERVICES[@]}-1; i>=0; i--)); do
    svc="${STOPPED_SERVICES[$i]}"
    systemctl start "$svc" \
      && echo "    $svc restarted" \
      || echo "    WARN: failed to start $svc — investigate"
  done
}
trap cleanup_on_exit EXIT

# --- pre-flight ---------------------------------------------------------
echo "==> Pre-flight"

zfs list rpool/services/sqlite >/dev/null 2>&1 \
  || { echo "ERROR: rpool/services/sqlite dataset missing — run earlier batch first" >&2; exit 1; }

[[ -d "$PLAYBOOK_DIR" ]] \
  || { echo "ERROR: PLAYBOOK_DIR=$PLAYBOOK_DIR not found" >&2; exit 1; }

for entry in "${SERVICES[@]}"; do
  IFS=: read -r svc owner group src_dir dbs <<<"$entry"
  systemctl cat "$svc" >/dev/null 2>&1 \
    || { echo "ERROR: systemd unit '$svc' not found" >&2; exit 1; }
  id -u "$owner" >/dev/null 2>&1 \
    || { echo "ERROR: owner '$owner' missing" >&2; exit 1; }
done

systemctl cat plex >/dev/null 2>&1 \
  || { echo "ERROR: plex unit not found" >&2; exit 1; }

confirm "Stop ${#SERVICES[@]} services + plex and migrate their SQLite DBs onto the 4K dataset?"

# --- phase 1: stop services --------------------------------------------
echo
echo "==> Phase 1: stop services"
for entry in "${SERVICES[@]}" "plex:::dummy:dummy"; do
  IFS=: read -r svc _ _ _ _ <<<"$entry"
  if systemctl is-active --quiet "$svc"; then
    echo "    stopping $svc"
    STOPPED_SERVICES+=("$svc")
    systemctl stop "$svc"
  else
    echo "    $svc not active — skipping"
  fi
done

# --- phase 2 + 3: mkdir + mv -------------------------------------------
echo
echo "==> Phase 2/3: create sqlite dirs + move DBs"
for entry in "${SERVICES[@]}"; do
  IFS=: read -r svc owner group src_dir dbs <<<"$entry"
  dst_dir="/mnt/services/sqlite/$svc"

  # Paperless uses 0700; everything else 0755.
  if [[ "$svc" == paperless ]]; then
    install -d -o "$owner" -g "$group" -m 0700 "$dst_dir"
  else
    install -d -o "$owner" -g "$group" -m 0755 "$dst_dir"
  fi

  IFS=, read -r -a db_list <<<"$dbs"
  for db in "${db_list[@]}"; do
    for ext in "" "-wal" "-shm"; do
      src="${src_dir}/${db}${ext}"
      dst="${dst_dir}/${db}${ext}"
      if [[ -L "$src" ]]; then
        # Already a symlink (re-run); skip.
        continue
      fi
      if [[ -f "$src" ]]; then
        echo "    mv $src -> $dst"
        mv "$src" "$dst"
      fi
    done
  done
done

# # Jellyfin's introskipper plugin DB (optional)
# if [[ -d /mnt/services/jellyfin/data/introskipper ]]; then
#   iskip_src=/mnt/services/jellyfin/data/introskipper
#   iskip_dst=/mnt/services/sqlite/jellyfin
#   for ext in "" "-wal" "-shm"; do
#     src="${iskip_src}/introskipper.db${ext}"
#     dst="${iskip_dst}/introskipper.db${ext}"
#     if [[ -L "$src" ]]; then
#       continue
#     fi
#     if [[ -f "$src" ]]; then
#       echo "    mv $src -> $dst"
#       mv "$src" "$dst"
#     fi
#   done
# fi

# Plex uses a directory bind (Databases/ is dedicated to SQLite). mv all
# files at the top of that dir — both live DBs and Plex's dated full-DB
# backups (com.plexapp.plugins.library.{db,blobs.db}-YYYY-MM-DD).
plex_src='/mnt/services/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases'
plex_dst=/mnt/services/sqlite/plex
if [[ -d "$plex_src" ]]; then
  install -d -o plex -g media -m 0755 "$plex_dst"
  shopt -s nullglob
  for f in "$plex_src"/*; do
    [[ -f "$f" ]] || continue
    echo "    mv $f -> $plex_dst/"
    mv "$f" "$plex_dst/"
  done
  shopt -u nullglob
fi

# --- phase 4: ansible apply --------------------------------------------
echo
echo "==> Phase 4: ansible-playbook (creates symlinks, updates units, restarts)"
cd "$PLAYBOOK_DIR"
mise run ansible --limit "$HOST" --tags "$ROLE_TAGS"

# --- phase 5: safety-net start -----------------------------------------
echo
echo "==> Phase 5: ensure all services are running"
for entry in "${SERVICES[@]}" "plex:::dummy:dummy"; do
  IFS=: read -r svc _ _ _ _ <<<"$entry"
  systemctl is-active --quiet "$svc" || systemctl start "$svc"
done

STOPPED_SERVICES=()
SUCCESS=true

# --- verify -------------------------------------------------------------
echo
echo "==> Done. Sanity checks:"
echo "    zfs list -o name,used,referenced,recordsize,compressratio rpool/services/sqlite"
zfs list -o name,used,referenced,recordsize,compressratio rpool/services/sqlite
echo
echo "    Live DB sizes on the 4K dataset:"
du -sh /mnt/services/sqlite/*/ | sort -h
echo
echo "    Snapshot growth: watch 'zfs list -t snapshot rpool/services | tail -7' over the next week."
echo "    Daily USED column should drop noticeably for the affected DBs."

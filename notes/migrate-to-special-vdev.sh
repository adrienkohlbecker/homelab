#!/usr/bin/env bash
# Migrate one tank dataset through the new special vdev via zfs send|recv.
# Run as root (sudo). Run inside screen/tmux — phase 1 may take hours.
#
# Usage: ./migrate-to-special-vdev.sh <eckwersheim|brumath|data>
#
# Phases:
#   1. snapshot @premigrate1, full send -R to tank/<ds>_new (services stay up)
#   2. confirm; stop services; snapshot @premigrate2; incremental send;
#      byte-equality check; atomic rename swap; restart services
#   3. verify (live file count, special-vdev growth, dataset properties)
#
# tank/<ds>_old is preserved as a safety net. Destroy manually when confident:
#   sudo zfs destroy -r tank/<ds>_old

set -euo pipefail

DS="${1:?usage: $0 <eckwersheim|brumath|data|media>}"
case "$DS" in
  eckwersheim|brumath|data|media) ;;
  *) echo "unknown dataset: $DS (allowed: eckwersheim brumath data media)" >&2; exit 2 ;;
esac

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

SRC="tank/$DS"
DST="tank/${DS}_new"
SNAP1="${SRC}@premigrate1"
SNAP2="${SRC}@premigrate2"
MNT="/mnt/$DS"

# Per-dataset systemd coordinator unit. The coordinator is a oneshot with
# RemainAfterExit=true that runs zfs_check_mount at boot; consumer services
# declare Requires=<coord> (smbd via the samba role's drop-in override). We
# auto-discover the full set from the reverse dep graph.
# Precondition: the samba/data/media ansible roles must have been applied so
# the coordinator units exist and smbd's override is installed.
declare -A DATASET_COORDINATOR=(
  [data]="zfs_mount_mnt_data.service"
  [brumath]="zfs_mount_mnt_brumath.service"
  [eckwersheim]="zfs_mount_mnt_eckwersheim.service"
  [media]="zfs_mount_mnt_media.service"
)

discover_dependents() {
  systemctl list-dependencies --reverse --plain "$1" 2>/dev/null \
    | awk -v self="$1" '/\.service$/ && $1 != self {print $1}'
}

COORD="${DATASET_COORDINATOR[$DS]}"
SERVICES_TO_STOP=$(discover_dependents "$COORD" | tr '\n' ' ')

confirm() {
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
}

get_special_alloc() {
  local v
  v=$(zpool list -Hp -v tank 2>/dev/null | awk '$1=="mirror-1"{print $2; exit}')
  echo "${v:-0}"
}

human_bytes() {
  numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "${1}B"
}

service_exists() {
  [[ "$(systemctl show -p LoadState --value "$1" 2>/dev/null)" == loaded ]]
}

# --- recovery trap ------------------------------------------------------
# If we exit before SUCCESS=true, attempt to restore service:
#   1. roll back partial rename (SRC gone, ${SRC}_old present)
#   2. ensure SRC is mounted
#   3. only restart services if SRC is mounted; otherwise leave them down and yell
SUCCESS=false
STOPPED_SERVICES=()

cleanup_on_exit() {
  set +e
  $SUCCESS && return 0
  echo
  echo "==> trap: attempting to restore service"

  if ! zfs list "$SRC" >/dev/null 2>&1 \
       && zfs list "${SRC}_old" >/dev/null 2>&1; then
    echo "    rolling back rename ${SRC}_old -> $SRC"
    zfs rename "${SRC}_old" "$SRC" 2>&1 \
      || echo "    WARN: rollback rename failed"
  fi

  if zfs list "$SRC" >/dev/null 2>&1; then
    if [[ "$(zfs get -H -o value mounted "$SRC" 2>/dev/null)" != "yes" ]]; then
      zfs mount "$SRC" 2>&1 || echo "    WARN: zfs mount $SRC failed"
    fi
  fi

  if [[ "$(zfs get -H -o value mounted "$SRC" 2>/dev/null)" == "yes" ]]; then
    # Restart in reverse order (smbd usually stopped first, started last)
    for ((i=${#STOPPED_SERVICES[@]}-1; i>=0; i--)); do
      svc="${STOPPED_SERVICES[$i]}"
      systemctl start "$svc" \
        && echo "    $svc restarted" \
        || echo "    WARN: failed to start $svc"
    done
  else
    echo "    ERROR: $SRC not mounted — services LEFT STOPPED:" >&2
    if [[ ${#STOPPED_SERVICES[@]} -gt 0 ]]; then
      printf '      %s\n' "${STOPPED_SERVICES[@]}" >&2
    fi
    echo "    Investigate manually before restarting them." >&2
  fi
}
trap cleanup_on_exit EXIT

# --- pre-flight ---------------------------------------------------------
echo "==> Pre-flight checks for $SRC"
echo "    services to stop in phase 2: ${SERVICES_TO_STOP:-(none)}"
zfs list "$SRC" >/dev/null

for s in "$SNAP1" "$SNAP2"; do
  if zfs list "$s" >/dev/null 2>&1; then
    echo "ERROR: snapshot $s exists — likely a partial prior run." >&2
    echo "To recover, run:" >&2
    echo "  sudo zfs destroy -r $DST   # if it exists" >&2
    echo "  sudo zfs destroy $SNAP1    # if it exists" >&2
    echo "  sudo zfs destroy $SNAP2    # if it exists" >&2
    echo "then re-run this script." >&2
    exit 1
  fi
done

if zfs list "$DST" >/dev/null 2>&1; then
  echo "WARNING: $DST exists (partial prior run?)"
  confirm "Overwrite with recv -F?"
fi

# Detect snapshot holds via the userrefs property (works on a dataset arg, unlike
# `zfs holds -r` which requires a snapshot). Any nonzero userrefs == held.
check_holds() {
  zfs get -H -o name,value -r -t snapshot userrefs "$SRC" 2>/dev/null \
    | awk '$2 != "0" && $2 != "-" {print}'
}
held=$(check_holds)
if [[ -n "$held" ]]; then
  echo "ERROR: $SRC has snapshot holds — rename would fail mid-swap:" >&2
  echo "$held" >&2
  exit 1
fi

# Verify each service unit exists (so the stop loop won't surprise us)
for svc in $SERVICES_TO_STOP; do
  if ! service_exists "$svc"; then
    echo "ERROR: systemd unit '$svc' not found — fix DATASET_SERVICES in the script." >&2
    exit 1
  fi
done

SPECIAL_BEFORE=$(get_special_alloc)
echo "    special vdev ALLOC before: $(human_bytes "$SPECIAL_BEFORE")"

# --- phase 1: async initial send ----------------------------------------
echo
echo "==> Phase 1: snapshot + full send (services up; may take hours)"
zfs snapshot "$SNAP1"
zfs send -Rv "$SNAP1" | zfs recv -uF "$DST"
echo "    phase 1 complete"

# --- phase 2: stop services, incremental, swap --------------------------
echo
echo "==> Phase 2: stop services, incremental catch-up, swap"
confirm "Ready to stop services [${SERVICES_TO_STOP}] and proceed?"

# Add to STOPPED_SERVICES *before* stopping so the trap will try to restart
# even if SIGKILL lands between the two operations.
for svc in $SERVICES_TO_STOP; do
  if systemctl is-active --quiet "$svc"; then
    echo "    stopping $svc"
    STOPPED_SERVICES+=("$svc")
    systemctl stop "$svc"
  else
    echo "    $svc not active — skipping"
  fi
done

zfs snapshot "$SNAP2"
zfs send -Rv -i "$SNAP1" "$SNAP2" | zfs recv -uF "$DST"

# Integrity check. Use logicalreferenced (uncompressed, allocation-class-invariant);
# referenced legitimately differs when blocks move from raidz2 to mirror.
SRC_REF=$(zfs get -Hp -o value referenced       "$SNAP2")
DST_REF=$(zfs get -Hp -o value referenced       "${DST}@premigrate2")
SRC_LREF=$(zfs get -Hp -o value logicalreferenced "$SNAP2")
DST_LREF=$(zfs get -Hp -o value logicalreferenced "${DST}@premigrate2")
echo "    referenced        (src)=$(human_bytes "$SRC_REF") (dst)=$(human_bytes "$DST_REF")"
echo "    logicalreferenced (src)=$(human_bytes "$SRC_LREF") (dst)=$(human_bytes "$DST_LREF")"

LREF_DIFF=$(( SRC_LREF > DST_LREF ? SRC_LREF - DST_LREF : DST_LREF - SRC_LREF ))
LREF_TOLERANCE=$(( SRC_LREF / 1000 ))   # 0.1%
if (( LREF_DIFF > LREF_TOLERANCE )); then
  echo "ERROR: logicalreferenced diff $(human_bytes "$LREF_DIFF") exceeds 0.1% of source — recv may have failed?" >&2
  exit 1
fi

# Diagnostic: surface any straggler holding /mnt/<ds> before unmount tries
lsof +D "$MNT" 2>/dev/null || true

zfs unmount "$SRC"

# Re-check holds: a hold placed during phase 1 (which may have run for hours)
# would not have been caught by the pre-flight check.
held=$(check_holds)
if [[ -n "$held" ]]; then
  echo "ERROR: $SRC acquired snapshot holds during phase 1 — rename would fail:" >&2
  echo "$held" >&2
  exit 1
fi

zfs rename "$SRC" "${SRC}_old"
zfs rename "$DST" "$SRC"

# _old is a safety-net copy, not a backup target. Detach it from autobackup
# before the next zfs_autobackup run, and clear its mountpoint so it doesn't
# claim /mnt/<ds>.
zfs inherit autobackup:bak "${SRC}_old"
zfs inherit mountpoint    "${SRC}_old"

# Set mountpoint on the new dataset; this also flips the source attribution
# from received -> local. Mount, then flip the other received properties so
# future ansible runs / zfs get reports match the role spec.
zfs set mountpoint="$MNT" "$SRC"
zfs mount "$SRC"

for prop in autobackup:bak special_small_blocks recordsize; do
  src=$(zfs get -H -o source "$prop" "$SRC")
  if [[ "$src" == "received" ]]; then
    val=$(zfs get -H -o value "$prop" "$SRC")
    echo "    flipping $prop=$val (received -> local)"
    zfs set "$prop=$val" "$SRC"
  fi
done

# Re-validate the new mount via the coordinator's zfs_check_mount oneshot.
# Catches: wrong mountpoint, readonly inherited, canmount=off, double mounts.
if [[ -n "$COORD" ]]; then
  echo "    re-validating mount via $COORD"
  systemctl restart "$COORD" \
    || { echo "ERROR: $COORD validation failed — mount likely misconfigured" >&2; exit 1; }
fi

# Restart services in reverse order
for ((i=${#STOPPED_SERVICES[@]}-1; i>=0; i--)); do
  svc="${STOPPED_SERVICES[$i]}"
  echo "    starting $svc"
  systemctl start "$svc"
done
STOPPED_SERVICES=()
SUCCESS=true

# --- phase 3: verify ----------------------------------------------------
echo
echo "==> Phase 3: verify"

ACTUAL=$(find "$MNT" -type f 2>/dev/null | wc -l)
echo "    live file count (post-swap, informational): $ACTUAL"

zfs list -o name,used,referenced,logicalused,recordsize,compressratio \
  "$SRC" "${SRC}_old"

SPECIAL_AFTER=$(get_special_alloc)
DELTA=$((SPECIAL_AFTER - SPECIAL_BEFORE))
echo "    special vdev ALLOC: before=$(human_bytes "$SPECIAL_BEFORE")" \
     "after=$(human_bytes "$SPECIAL_AFTER")" \
     "delta=$(human_bytes "$DELTA")"
if (( DELTA <= 0 )); then
  echo "    WARN: special vdev did not absorb metadata — investigate" >&2
fi

echo
echo "==> Done. ${SRC}_old retained. When confident, destroy with:"
echo "      sudo zfs destroy -r ${SRC}_old"

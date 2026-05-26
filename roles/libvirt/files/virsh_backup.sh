#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh
f_require_root

# Daily best-effort dump of libvirt's *persistent* definitions: every
# domain (plus the NVRAM varstore its XML only references by path), every
# virtual network, and every storage pool -- so a rebuilt host can
# re-`define` what it ran. Disk *images* are out of scope here (see
# notes/SOMEDAY.md "Back up libvirt VMs and their definitions"); this is
# a config safety net, not disaster recovery, and it currently rides the
# same un-replicated rpool/libvirt dataset it dumps into.
#
# functions.sh installs set -Eeuo pipefail + an ERR trap and exposes the
# f_failed counter. We isolate per-object failures with explicit if/else
# (an if-condition is exempt from errexit and the ERR trap) and bump
# f_failed instead of aborting, so one bad object doesn't lose the rest of
# the run; a non-zero f_failed propagates to the timer at the end so the
# systemdunits collector sees the failed unit. Same loop-isolate-propagate
# shape as roles/zfs_autobackup/files/zfs_backup_offsite.sh.
backup_dir=/var/lib/libvirt/images

# Domain XML can reference NVRAM/secret paths, and `dumpxml --security-info`
# (below) un-redacts <graphics passwd=...> so a credentialed guest is
# actually restorable -- keep every dump root-owned and mode 0600.
umask 077

# Empty globs vanish, so the prune loops below iterate real files only.
shopt -s nullglob

# Collapse an object name to a safe filename charset: strip any leading
# path component, then replace anything outside [A-Za-z0-9._-] with `_`.
safe_name() {
  printf '%s' "${1##*/}" | tr -c 'A-Za-z0-9._-' '_'
}

# Destinations written this run. Doubles as (a) an intra-namespace
# collision guard -- two objects whose names sanitize to the same file
# would otherwise silently clobber each other -- and (b) the keep-set the
# prune loops diff against to drop dumps for objects that no longer exist.
declare -A seen=()

# dump_obj DEST CMD... : run CMD with stdout staged to a temp file beside
# DEST, then atomically rename onto DEST -- so a partial, failed, or empty
# dump never clobbers the last good copy. Refuses a DEST already written
# this run. Records failures in f_failed; always returns 0 so the caller's
# errexit is safe.
dump_obj() {
  local dest=$1
  shift
  local tmp
  if [ -n "${seen[$dest]:-}" ]; then
    echo >&2 "refusing to overwrite $dest -- two objects sanitize to the same name"
    ((f_failed += 1))
    return
  fi
  tmp=$(mktemp "$dest.XXXXXX") || {
    echo >&2 "mktemp failed for $dest"
    ((f_failed += 1))
    return
  }
  # `[ -s ]`: a virsh hiccup can exit 0 yet write nothing, and an empty
  # dump is not a good backup -- treat it as failure and keep the prior copy.
  if "$@" >"$tmp" && [ -s "$tmp" ]; then
    mv -f "$tmp" "$dest"
    seen[$dest]=1
  else
    rm -f "$tmp"
    echo >&2 "dump failed (or empty) for $dest:$(printf ' %q' "$@")"
    ((f_failed += 1))
  fi
}

# dump_kind PREFIX DUMPVERB LISTCMD... : dump every persistent object of
# one kind, then prune stale dumps. --inactive captures the persistent
# (next-boot) config, not the runtime XML with host-specific hotplug/CPU
# augmentation; --persistent skips transient objects (no --inactive
# definition to dump). The prune runs only when the listing succeeded, so
# a libvirtd outage (empty list != "no objects") can't wipe the backup set.
dump_kind() {
  local prefix=$1 dumpverb=$2
  shift 2
  local objs name f
  if ! objs=$("$@"); then
    echo >&2 "listing failed:$(printf ' %q' "$@")"
    ((f_failed += 1))
    return
  fi
  while read -r name; do
    [ -n "$name" ] || continue
    dump_obj "$backup_dir/${prefix}_$(safe_name "$name").xml" \
      virsh "$dumpverb" --inactive "$name"
  done <<<"$objs"
  for f in "$backup_dir/${prefix}_"*.xml; do
    [ -n "${seen[$f]:-}" ] || {
      echo >&2 "pruning stale dump: $f"
      rm -f -- "$f"
    }
  done
}

# Domains need a dedicated loop: --security-info un-redacts secret fields,
# and each domain may carry an NVRAM varstore that dumpxml records only by
# path. The atomic write, collision guard, and empty-guard are shared via
# dump_obj. The listing is captured first (not piped) so a failed enumerate
# bumps f_failed instead of silently iterating zero times.
if domains=$(virsh list --all --persistent --name); then
  while read -r domain; do
    [ -n "$domain" ] || continue
    safe=$(safe_name "$domain")
    dest="$backup_dir/dom_$safe.xml"
    dump_obj "$dest" virsh dumpxml --inactive --security-info "$domain"
    # Only chase the NVRAM path if the XML dump itself landed.
    [ -n "${seen[$dest]:-}" ] || continue

    # The NVRAM varstore holds UEFI / Secure Boot state (e.g. the macOS
    # guest's OpenCore + iCloud activation) that dumpxml records only by
    # path. Read the path with a real XML parser over the dump rather than
    # a regex -- robust to attribute reordering. (`virsh --xpath` would be
    # the native option but its string() support varies by libvirt version
    # -- it exits 1 on jammy's 8.0; ElementTree behaves the same on every
    # release.) `|| nvram=` keeps a parse miss from tripping set -e.
    nvram=$(python3 -c 'import sys, xml.etree.ElementTree as ET; n = ET.parse(sys.argv[1]).find("./os/nvram"); print((n.text or "") if n is not None else "")' "$dest" 2>/dev/null) || nvram=
    [ -n "$nvram" ] && [ -f "$nvram" ] || continue

    # The path is domain-controlled, so realpath it and admit only
    # libvirt's varstore directory before a root-run cp reads it: a crafted
    # <nvram> pointing elsewhere -- including back into $backup_dir -- would
    # otherwise exfiltrate a root-readable file or poison the backup set.
    nvram_real=$(realpath -e -- "$nvram") || {
      echo >&2 "cannot resolve nvram for $domain: $nvram"
      ((f_failed += 1))
      continue
    }
    case "$nvram_real" in
    /var/lib/libvirt/qemu/nvram/*)
      # Stage + rename like the XML dump so a crash mid-copy can't truncate
      # the last good varstore. cp --reflink=auto is a near-free CoW clone
      # on the ZFS image dataset, a full copy elsewhere. chmod 0600 the temp
      # explicitly: it is already 0600 under umask 077, but pin it so a
      # future umask change can't leak a secret-bearing varstore.
      vdest="$backup_dir/dom_${safe}_VARS.fd"
      vtmp=$(mktemp "$vdest.XXXXXX") || {
        echo >&2 "mktemp failed for $vdest"
        ((f_failed += 1))
        continue
      }
      if cp --reflink=auto -- "$nvram_real" "$vtmp"; then
        chmod 0600 "$vtmp"
        mv -f "$vtmp" "$vdest"
        seen[$vdest]=1
      else
        rm -f "$vtmp"
        echo >&2 "nvram copy failed for $domain: $nvram_real"
        ((f_failed += 1))
      fi
      ;;
    *)
      echo >&2 "refusing out-of-tree nvram for $domain: $nvram_real"
      ((f_failed += 1))
      ;;
    esac
  done <<<"$domains"
  # Prune stale domain dumps + varstores (listing succeeded above).
  for f in "$backup_dir/dom_"*.xml "$backup_dir/dom_"*_VARS.fd; do
    [ -n "${seen[$f]:-}" ] || {
      echo >&2 "pruning stale dump: $f"
      rm -f -- "$f"
    }
  done
else
  echo >&2 "virsh list failed"
  ((f_failed += 1))
fi

dump_kind net net-dumpxml virsh net-list --all --persistent --name
dump_kind pool pool-dumpxml virsh pool-list --all --persistent --name

[ "$f_failed" -eq 0 ] || exit 1

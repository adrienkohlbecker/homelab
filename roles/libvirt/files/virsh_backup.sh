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
# functions.sh installs set -Eeuo pipefail + an ERR trap. We override
# per-object failure with explicit if/else so one bad domain doesn't
# abort the rest of the run; the ERR trap still breadcrumbs each, and a
# non-zero $failed propagates to the timer so the systemdunits collector
# sees the failed unit.
backup_dir=/var/lib/libvirt/images

# Domain XML can reference NVRAM/secret paths and (for a future guest) a
# <graphics passwd=...>; keep every dump root-owned and mode 0600.
umask 077

failed=0

# Write `virsh <verb> --inactive <name>` to <dest> atomically. `>` opens
# and truncates the target before virsh runs, so a partial/failed dump
# would clobber the last good copy -- stage to a temp file, rename only
# on success. --inactive captures the persistent (next-boot) config, not
# the runtime XML with host-specific hotplug/CPU augmentation. Always
# returns 0 (failure recorded in $failed) so the caller's set -e is safe.
dump() {
  local verb=$1 name=$2 dest=$3 tmp
  tmp=$(mktemp "$dest.XXXXXX")
  if virsh "$verb" --inactive "$name" >"$tmp"; then
    mv -f "$tmp" "$dest"
  else
    rm -f "$tmp"
    echo >&2 "virsh $verb failed for: $name"
    failed=1
  fi
}

# Process substitution (not a pipe) so the loop body runs in this shell
# and updates $failed. Plain `read -r` (default IFS) trims the trailing
# whitespace `virsh ... --name` pads onto names, and the emptiness guard
# drops the trailing blank line; names flow into paths, so strip any
# directory component (${name##*/}) defensively.
while read -r domain; do
  [ -n "$domain" ] || continue
  safe=${domain##*/}
  dest="$backup_dir/$safe.xml"
  tmp=$(mktemp "$dest.XXXXXX")
  if virsh dumpxml --inactive "$domain" >"$tmp"; then
    # The NVRAM varstore holds UEFI / Secure Boot state (e.g. the macOS
    # guest's OpenCore + iCloud activation) that dumpxml records only by
    # path. Extract the path from the XML we just captured and copy the
    # file itself alongside the definition.
    nvram=$(sed -n 's:.*<nvram[^>]*>\([^<]*\)</nvram>.*:\1:p' "$tmp")
    mv -f "$tmp" "$dest"
    if [ -n "$nvram" ] && [ -f "$nvram" ]; then
      cp -a "$nvram" "$backup_dir/${safe}_VARS.fd" || failed=1
    fi
  else
    rm -f "$tmp"
    echo >&2 "virsh dumpxml failed for domain: $domain"
    failed=1
  fi
done < <(virsh list --all --name)

while read -r net; do
  [ -n "$net" ] || continue
  dump net-dumpxml "$net" "$backup_dir/net_${net##*/}.xml"
done < <(virsh net-list --all --name)

while read -r pool; do
  [ -n "$pool" ] || continue
  dump pool-dumpxml "$pool" "$backup_dir/pool_${pool##*/}.xml"
done < <(virsh pool-list --all --name)

exit "$failed"

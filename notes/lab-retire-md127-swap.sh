#!/usr/bin/env bash
# lab-retire-md127-swap.sh — drop the legacy 1.5 GiB md127 raid0 swap on
# /dev/nvme[012]n1p2 once the new zvol swap (dozer/swap) is active.
#
# md127 is raid0, so a single NVMe failure panics the host on the next
# swap-in; it's also constantly full (1.5G/1.5G), so it's not earning its
# keep as a deadlock-mitigation priority swap. This retires it cleanly:
#
#   1. verify a replacement swap is active and has the headroom
#      to absorb the pages currently sitting on md127
#   2. swapoff md127 (may take minutes — kernel migrates anon pages to
#      the remaining swap device, paging some back into RAM)
#   3. remove the fstab entry
#   4. stop and wipe the array (so it doesn't reassemble at next boot)
#   5. drop the ARRAY line from /etc/mdadm/mdadm.conf
#   6. update-initramfs so the early-boot mdadm assembly stops looking
#      for it
#
# Run on lab as root. Idempotent: each step skips if the previous run
# already removed it.
set -euo pipefail

ARRAY=/dev/md127
MDADM_CONF=/etc/mdadm/mdadm.conf
FSTAB=/etc/fstab

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

pause() { read -rp ">>> $1 — press enter to continue (ctrl-c to abort) "; }

#######################################################################
# Step 1: pre-flight
#######################################################################
echo "==> Pre-flight checks"

if ! [[ -b $ARRAY ]]; then
    echo "    $ARRAY does not exist — already retired? Continuing to cleanup steps."
    array_present=0
else
    array_present=1
fi

# How many bytes does md127 currently hold?
if [[ $array_present -eq 1 ]]; then
    md_used_kb=$(awk -v dev="${ARRAY##*/}" '$1 == dev {print $4}' /proc/swaps)
    md_size_kb=$(awk -v dev="${ARRAY##*/}" '$1 == dev {print $3}' /proc/swaps)
else
    md_used_kb=0
    md_size_kb=0
fi

# Sum of (size - used) across all OTHER active swaps.
other_free_kb=$(awk -v skip="${ARRAY##*/}" '
    NR > 1 && $1 != skip { free += $3 - $4 }
    END { print free + 0 }
' /proc/swaps)

# Free RAM (kB)
mem_avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)

echo "    md127 holds:          ${md_used_kb} kB (of ${md_size_kb} kB)"
echo "    other swap free:      ${other_free_kb} kB"
echo "    MemAvailable:         ${mem_avail_kb} kB"

if [[ $array_present -eq 1 && $md_used_kb -gt 0 ]]; then
    headroom_kb=$(( other_free_kb + mem_avail_kb / 2 ))
    if (( headroom_kb < md_used_kb )); then
        echo "    REFUSING: not enough headroom to absorb md127's pages." >&2
        echo "    Need ${md_used_kb} kB; have ${headroom_kb} kB (other_swap_free + MemAvailable/2)." >&2
        echo "    Activate the replacement swap (zvol or otherwise) first." >&2
        exit 1
    fi
fi

if [[ $array_present -eq 1 ]] && (( other_free_kb == 0 )); then
    echo "    WARNING: no other active swap. Retiring md127 leaves the host swapless"
    echo "             until /dev/zvol/dozer/swap (or equivalent) is on."
    pause "proceed anyway?"
fi

#######################################################################
# Step 2: swapoff
#######################################################################
if [[ $array_present -eq 1 ]] && grep -q "^${ARRAY} " /proc/swaps; then
    echo "==> swapoff $ARRAY (may take several minutes if heavily used)"
    swapoff "$ARRAY"
else
    echo "==> swapoff: already off"
fi

#######################################################################
# Step 3: fstab
#######################################################################
echo "==> Removing fstab entries pointing at md/swap"
# Match either /dev/md127 or the by-id /dev/md/swap symlink.
if grep -E '^[[:space:]]*/dev/md(127|/swap)[[:space:]]' "$FSTAB" >/dev/null; then
    cp -a "$FSTAB" "${FSTAB}.bak.$(date +%s)"
    sed -i -E '/^[[:space:]]*\/dev\/md(127|\/swap)[[:space:]]/d' "$FSTAB"
    echo "    fstab updated (backup written)"
else
    echo "    fstab already clean"
fi

#######################################################################
# Step 4: stop and wipe the array
#######################################################################
if [[ $array_present -eq 1 ]]; then
    members=$(awk -v arr="${ARRAY##*/}" '
        $1 == arr ":" || $1 == arr {
            for (i = 5; i <= NF; i++) {
                gsub(/\[[0-9]+\]/, "", $i)
                print "/dev/" $i
            }
        }
    ' /proc/mdstat)

    echo "==> Discovered members of $ARRAY:"
    for m in $members; do
        # Show partition + parent disk model/serial so the operator can
        # eyeball that we're not about to nuke the wrong device.
        parent=$(lsblk -ndo PKNAME "$m" 2>/dev/null || true)
        info=$(lsblk -ndo SIZE,MODEL,SERIAL "/dev/$parent" 2>/dev/null || echo "?")
        echo "    $m   (on /dev/$parent — $info)"
    done
    pause "wipefs --all the partitions listed above?"

    echo "==> Stopping $ARRAY"
    mdadm --stop "$ARRAY"

    echo "==> Wiping md superblocks from members"
    for m in $members; do
        echo "    wipefs $m"
        wipefs --all "$m"
    done
else
    echo "==> Array already stopped; skipping wipe (re-run after `mdadm --assemble` if you need to wipe)"
    members=""
fi

#######################################################################
# Step 5: mdadm.conf
#######################################################################
echo "==> Cleaning $MDADM_CONF"
if grep -E "^ARRAY[[:space:]]+${ARRAY}([[:space:]]|$)" "$MDADM_CONF" >/dev/null 2>&1; then
    cp -a "$MDADM_CONF" "${MDADM_CONF}.bak.$(date +%s)"
    sed -i -E "/^ARRAY[[:space:]]+${ARRAY//\//\\/}([[:space:]]|$)/d" "$MDADM_CONF"
    echo "    removed ARRAY line for $ARRAY"
else
    echo "    no ARRAY line for $ARRAY"
fi

#######################################################################
# Step 6: initramfs
#######################################################################
echo "==> Rebuilding initramfs so early-boot mdadm stops scanning for $ARRAY"
update-initramfs -u -k all

#######################################################################
# Verification
#######################################################################
echo
echo "==> Done. Verify:"
echo
swapon --show
echo
cat /proc/mdstat
echo
echo "    Expected: $ARRAY absent from both. /dev/zvol/dozer/swap (or"
echo "    your replacement) should be the only active swap."
echo
echo "    The retired p2 partitions (likely /dev/nvme[012]n1p2) are now"
echo "    free for reuse — leave them, or repurpose later."

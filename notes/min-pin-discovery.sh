#!/usr/bin/env bash
# Discover the minimal /etc/apt/preferences.d/ pin set required to upgrade
# a given seed package set from jammy to noble.
#
# Usage: discover.sh <seed-pkg> [<seed-pkg> ...]
#
# Examples:
#   discover.sh zfsutils-linux zfs-initramfs zfs-zed
#   discover.sh podman buildah skopeo
#
# Procedure:
#   1. Add noble apt sources (using the same nexus mirror jammy is on).
#   2. Write a pin file listing only the seed set.
#   3. Run apt-get -s install on the pin set and parse two things:
#        a. "<pkg> : Depends: <dep> ... but ..." lines (unmet deps) — add
#           both <pkg> and <dep> to the pin set.
#        b. "The following packages will be REMOVED:" block — add those to
#           the pin set too. The resolver silently drops compatible-but-
#           unpinned packages (e.g. locales) to make the install fit, so
#           "no unmet deps" is not enough to call convergence.
#   4. Loop until both lists are empty (true convergence).
#   5. Run a real install at the end to confirm it actually proceeds.
#
# Run as root on the kept harness VM. The script never touches files
# outside /etc/apt/{preferences,sources}.d/ and /tmp.

set -euo pipefail

if [ $# -lt 1 ]; then
    sed -n '2,9p' "$0" | sed 's/^#//; s/^ //'
    exit 2
fi

SEED="$*"

MIRROR="http://nexus.lab.fahm.fr/repository/ubuntu-ports"
PIN=/etc/apt/preferences.d/backports-discovery
SRC=/etc/apt/sources.list.d/noble-backports.sources

write_sources() {
    cat > "$SRC" <<EOF
Types: deb
URIs: $MIRROR/
Suites: noble noble-updates noble-backports noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
}

# Return the set of virtual package names a real package Provides:.
# Used to spot alternative replacements: if pkg P is being removed and
# some package we're installing Provides any of P's virtuals (and vice
# versa), P is being intentionally replaced (e.g. chrony replaces
# systemd-timesyncd, both Provide+Conflict `time-daemon`). Pinning P
# to noble in that case is wrong — apt should be allowed to drop it.
#
# Provides: line format: "Provides: foo, bar (= 1.0), baz"
# Output: one virtual name per line, version annotations stripped.
get_provides() {
    local pkg="$1"
    local show
    show=$(apt-cache show "$pkg" 2>/dev/null || true)
    [ -z "$show" ] && return 0
    # Capture-then-parse: awk's `exit` would SIGPIPE apt-cache, but
    # `echo "$show"` finishes immediately so there's no producer to
    # SIGPIPE. Trailing `|| true` swallows grep's no-match return code.
    echo "$show" \
        | awk -F': ' '/^Provides: / { print $2; exit }' \
        | tr ',' '\n' \
        | sed 's/[[:space:]]*(.*)//;s/^[[:space:]]*//;s/[[:space:]]*$//' \
        | grep -v '^$' || true
}

write_pin() {
    local pkgs="$1"
    cat > "$PIN" <<EOF
Package: $pkgs
Pin: release n=noble
Pin-Priority: 991

Package: *
Pin: release n=jammy
Pin-Priority: 500

Package: *
Pin: release o=Ubuntu
Pin-Priority: -10
EOF
}

main() {
    echo "==> Discovering minimal pin for seed=[$SEED]"
    echo "==> Writing noble sources"
    write_sources
    apt-get -qq update

    pin_set="$SEED"
    iter=0
    while :; do
        iter=$((iter + 1))
        echo
        echo "==> Iter $iter: pinning [$pin_set]"
        write_pin "$pin_set"
        apt-get -qq update

        # -s simulates; plain `install` (not --only-upgrade) so the t64
        # rename transitions (libssl3 -> libssl3t64) actually drag in the
        # noble package. The pin keeps anything not explicitly listed at
        # jammy or rejected.
        out=$(apt-get -s install -y $pin_set 2>&1 || true)
        # Apt unmet-dep lines look like:
        #   <subject> : Depends: <dep> (...) but ... is to be installed
        # We add BOTH:
        #   - <subject>: needed when an installed jammy pkg's "<X" constraint
        #     blocks the noble libc/libssl upgrade (e.g. libc-bin pinned to
        #     libc6 (<2.36)). Upgrading subject to noble loosens the bound.
        #   - <dep>: needed when subject (already in pin) names a noble-only
        #     dep that isn't yet pinned (e.g. zfsutils-linux -> libssl3t64).
        #
        # Apt's "REMOVED" block lists packages the resolver decided to drop
        # to make the install fit. That doesn't surface as an "unmet" error,
        # so the loop's exit branch can declare convergence while a critical
        # package (e.g. locales) silently disappears. Treat removals as
        # additional pin candidates so the next iter forces the noble
        # version, then re-resolves.
        unmet=$(echo "$out" | awk '
            /^ +[a-zA-Z0-9.+:-]+ : (Depends|PreDepends):/ {
                gsub(/:$/, "", $1)
                print $1                        # subject (left of " : ")
                for (i=1; i<=NF; i++) if ($i == "Depends:" || $i == "PreDepends:") { print $(i+1); break }
            }
        ' | sort -u)
        # Removed packages without a noble candidate are renamed-away (t64
        # rename: libssl3 -> libssl3t64). Pinning them is wrong: noble has
        # no candidate so the pin no-ops, but listing them in the final
        # apt install commits us to keeping them installed, which collides
        # head-on with libssl3t64's `Breaks: libssl3`. Drop those — the
        # t64-renamed replacement gets pulled via the unmet-deps branch
        # already.
        removed_raw=$(echo "$out" | awk '
            /^The following packages will be REMOVED:/ { capture=1; next }
            /^The following / { capture=0 }
            /^[A-Z0-9]/      { capture=0 }
            capture && /^ +/ {
                for (i=1; i<=NF; i++) {
                    name=$i
                    sub(/\*$/, "", name)        # apt marks autoinstalled with *
                    if (name != "") print name
                }
            }
        ' | sort -u)
        # Capture madison output into a variable before grepping. With
        # `set -o pipefail`, `apt-cache madison foo | grep -q noble` is
        # broken: grep -q exits on first match, madison gets SIGPIPE and
        # exits 141, pipefail marks the pipeline failed, the surrounding
        # `if` takes the false branch — so a noble match falsely drops
        # the package. Splitting the pipeline avoids the SIGPIPE race.
        #
        # Section: metapackages (ubuntu-minimal, ubuntu-server, ...) are
        # marker pkgs with no real files. Apt removes them when their
        # transitive deps lose satisfiability, but pinning them to noble
        # activates the noble metapackage's Depends list, which on
        # ubuntu-minimal pulls dhcpcd-base + dracut-install via the
        # initramfs-tools chain. Drop them — accept the marker's removal.
        # Cache pin_set Provides once per iter — get_provides is cheap
        # but called O(removed × pin) times below and apt-cache show's
        # subprocess cost dominates.
        pin_provides=""
        for installed in $pin_set; do
            for v in $(get_provides "$installed"); do
                pin_provides="$pin_provides $v"
            done
        done
        removed=""
        removed_no_noble=""
        removed_meta=""
        removed_replaced=""
        for pkg in $removed_raw; do
            mad=$(apt-cache madison "$pkg" 2>/dev/null || true)
            if ! echo "$mad" | grep -q noble; then
                removed_no_noble="$removed_no_noble $pkg"
                continue
            fi
            # Capture-then-parse to avoid the same SIGPIPE trap as the
            # madison filter: `apt-cache show | awk '... exit'` makes
            # apt-cache take SIGPIPE when awk exits early, pipefail
            # bubbles 141, set -e kills the whole script silently.
            show=$(apt-cache show "$pkg" 2>/dev/null || true)
            section=$(echo "$show" | awk -F': ' '/^Section: / { print $2; exit }')
            if [ "$section" = "metapackages" ]; then
                removed_meta="$removed_meta $pkg"
                continue
            fi
            # Alternative-replacement: if pkg P being removed shares any
            # virtual with something in pin_set, apt is replacing it
            # rather than failing. e.g. chrony Provides time-daemon and
            # systemd-timesyncd Provides time-daemon — installing chrony
            # forces systemd-timesyncd out, and pinning systemd-timesyncd
            # to noble would just chase a doomed install.
            replaced_via=""
            for v in $(get_provides "$pkg"); do
                case " $pin_provides " in
                    *" $v "*) replaced_via="$v"; break ;;
                esac
            done
            if [ -n "$replaced_via" ]; then
                removed_replaced="$removed_replaced $pkg(via $replaced_via)"
                continue
            fi
            removed="$removed $pkg"
        done
        # Trim leading whitespace
        removed=$(echo "$removed" | sed 's/^ *//')
        [ -n "$removed_no_noble" ] && echo "    - (filtered, no noble candidate):$removed_no_noble"
        [ -n "$removed_meta" ]     && echo "    - (filtered, metapackage):$removed_meta"
        [ -n "$removed_replaced" ] && echo "    - (filtered, alternative):$removed_replaced"
        # Convergence check: both filtered lists empty → done. Filtered
        # (not raw) because t64-renamed removals and metapackage drops
        # are accepted final-state outcomes, not work to do.
        new=$(printf "%s\n%s\n" "$unmet" "$removed" | sort -u | sed "/^$/d")
        if [ -z "$new" ]; then
            if echo "$out" | grep -qE "0 upgraded, 0 newly installed.*0 to remove"; then
                echo "    apt: nothing to do."
            else
                echo "    apt: resolver converged; would proceed."
            fi
            break
        fi
        added_unmet=""
        added_removed=""
        for p in $new; do
            case " $pin_set " in
                *" $p "*) continue ;;
            esac
            pin_set="$pin_set $p"
            # Tag where this package surfaced from so the iteration log
            # makes the chrony/locales-style silent-removal cases obvious.
            if printf "%s\n" "$unmet" | grep -qx -- "$p"; then
                added_unmet="$added_unmet $p"
            else
                added_removed="$added_removed $p"
            fi
        done
        if [ -z "$added_unmet$added_removed" ]; then
            echo "    no fresh packages to add; resolver still unhappy."
            echo "$out" | tail -30
            break
        fi
        [ -n "$added_unmet" ]   && echo "    + (unmet)  $added_unmet"
        [ -n "$added_removed" ] && echo "    + (removed)$added_removed"
        if [ "$iter" -gt 20 ]; then
            echo "    aborting: exceeded 20 iterations."
            break
        fi
    done

    # Post-convergence pruning. The main loop is greedy: anything apt
    # mentions in unmet/removed gets pinned. Some of those are over-
    # additions where jammy's version actually satisfies the constraint
    # (e.g. zfs-initramfs depends on initramfs-tools with no version, so
    # jammy's initramfs-tools is fine — pinning it to noble would
    # activate a Depends: dhcpcd-base, dracut-install cascade). For each
    # non-seed pin, try removing it and see if the simulate is still
    # clean; if so, jammy satisfies and the pin is unnecessary.
    #
    # Done as a fixed-point loop because removing one pin can make
    # another removable (the surviving constraints shift). Restart from
    # the beginning each time we drop one — order shouldn't matter for
    # correctness but it keeps the trace easy to read.
    echo
    echo "==> Pruning unnecessary pins (where jammy satisfies)..."
    pruned_any=1
    while [ "$pruned_any" -eq 1 ]; do
        pruned_any=0
        for p in $pin_set; do
            # Seeds are required by definition — never prune.
            case " $SEED " in *" $p "*) continue ;; esac
            test_set=$(printf '%s\n' $pin_set | grep -vx "$p" | xargs)
            write_pin "$test_set"
            apt-get -qq update
            test_out=$(apt-get -s install -y $test_set 2>&1 || true)
            # Clean = no unmet deps and no non-metapackage REMOVED. A
            # metapackage being dropped is always acceptable; a real
            # package being dropped means $p was load-bearing.
            if echo "$test_out" | grep -q "have unmet dependencies"; then
                continue
            fi
            removed_real=""
            for r in $(echo "$test_out" | awk '
                /^The following packages will be REMOVED:/ { capture=1; next }
                /^The following / { capture=0 }
                /^[A-Z0-9]/      { capture=0 }
                capture && /^ +/ {
                    for (i=1; i<=NF; i++) {
                        name=$i; sub(/\*$/, "", name)
                        if (name != "") print name
                    }
                }'); do
                show=$(apt-cache show "$r" 2>/dev/null || true)
                section=$(echo "$show" | awk -F': ' '/^Section: / { print $2; exit }')
                [ "$section" = "metapackages" ] && continue
                removed_real="$removed_real $r"
            done
            if [ -n "$removed_real" ]; then
                continue
            fi
            echo "    pruned (jammy satisfies): $p"
            pin_set="$test_set"
            pruned_any=1
            break
        done
    done
    write_pin "$pin_set"
    apt-get -qq update

    echo
    echo "==> Final pin set ($(echo $pin_set | wc -w) packages):"
    for p in $pin_set; do echo "  - $p"; done

    echo
    echo "==> Real apt-get install (no -s) to confirm:"
    # No --only-upgrade: t64 rename packages (libssl3 -> libssl3t64) are
    # NEW from dpkg's perspective and only land via plain install.
    apt-get install -y $pin_set || {
        echo "!! install failed"
        return 1
    }
}

main "$@"

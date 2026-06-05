#!/bin/bash
# Build-time hook run by build-init.sh inside the builder container (from
# ${BUILDROOT}/rc.d/*). Writes the dracut network cmdline fragment that
# recovery.conf bakes into the initramfs via install_items (a missing fragment
# hard-fails the build).
#
# Kept in /etc/cmdline.d (dracut reads it at boot) rather than the EFI bundle's
# embedded kernel cmdline: a duplicate ip= in the bundle makes dracut-network
# fail catastrophically (upstream remote-access docs). single-dhcp stops after
# the first interface succeeds (ip=dhcp tries all NICs, adding timeout on those
# without a DHCP server).
set -euo pipefail
mkdir -p /etc/cmdline.d
echo "ip=single-dhcp rd.neednet=1 rd.net.timeout.carrier=30" >/etc/cmdline.d/dracut-network.conf

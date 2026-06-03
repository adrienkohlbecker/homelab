#!/bin/bash
# Build-time hook run by build-init.sh inside the builder container (from
# ${BUILDROOT}/rc.d/*). Writes the dracut network cmdline fragment that
# dropbear.conf bakes into the initramfs via install_optional_items.
#
# Kept in /etc/cmdline.d (dracut reads it at boot) rather than the EFI bundle's
# embedded kernel cmdline: a duplicate ip=dhcp in the bundle makes
# dracut-network fail catastrophically (upstream remote-access docs).
set -euo pipefail
mkdir -p /etc/cmdline.d
echo "ip=dhcp rd.neednet=1" > /etc/cmdline.d/dracut-network.conf

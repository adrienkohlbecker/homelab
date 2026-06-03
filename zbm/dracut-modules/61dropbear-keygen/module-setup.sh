#!/bin/bash
# Custom dracut module: generate dropbear host keys at boot, not at build, so no
# host private key is ever baked into the (registry-stored) recovery image.
#
# Sourced by dracut at build time (like every module-setup.sh — no set -euo
# pipefail, which would break dracut's sourcing). Numbered 61 so it installs
# after 60crypt-ssh, letting install() remove the keys crypt-ssh just baked.

# called by dracut
check() {
  return 0
}

# called by dracut
depends() {
  # crypt-ssh installs dropbear + the 99-dropbear-start.sh hook we run ahead of
  echo crypt-ssh
  return 0
}

# called by dracut
install() {
  # dropbearkey is needed in the initramfs to mint keys at boot; crypt-ssh only
  # installs the dropbear server itself.
  inst_multiple dropbearkey

  # Drop the host keys crypt-ssh baked into the image, so no private key ships.
  rm -f "${initdir}/etc/dropbear/"dropbear_*_host_key

  # Regenerate at boot, at pre-udev 98 — before crypt-ssh's 99-dropbear-start.sh.
  inst_hook pre-udev 98 "${moddir}/regen-keys.sh"
}

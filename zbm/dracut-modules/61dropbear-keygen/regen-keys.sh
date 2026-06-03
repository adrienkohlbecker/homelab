#!/bin/sh
# Boot-time dracut hook (pre-udev 98): mint fresh dropbear host keys in the
# initramfs before crypt-ssh's 99-dropbear-start.sh serves them from the default
# /etc/dropbear paths. The image ships no host private key (61dropbear-keygen's
# install() deleted the baked ones), so keys exist only in this boot's tmpfs.
#
# /bin/sh (busybox), executed by the dracut event loop — deliberately no
# `set -e`: a transient keygen hiccup must not abort the whole recovery boot.
#
# Note: crypt-ssh.conf still carries the (now-deleted) build-time fingerprints,
# so the fingerprints 99-dropbear-start.sh prints to the boot log are stale.
# The keys actually served are these boot-generated ones.
for keytype in rsa ecdsa; do
  keyfile="/etc/dropbear/dropbear_${keytype}_host_key"
  [ -f "${keyfile}" ] && continue
  dropbearkey -t "${keytype}" -f "${keyfile}" >/dev/null 2>&1 \
    || echo "dropbear-keygen: ${keytype} host key generation failed" >&2
done

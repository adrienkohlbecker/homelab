#!/bin/bash

check() {
  return 0
}

depends() {
  echo shutdown
  return 0
}

install() {
  # shellcheck disable=SC2154  # initdir is provided by dracut.
  rm -f \
    "${initdir}/usr/bin/firmware-setup" \
    "${initdir}/usr/bin/poweroff" \
    "${initdir}/usr/bin/reboot" \
    "${initdir}/usr/bin/shutdown"
}

#!/bin/bash
set -euo pipefail

# ffmpeg supplies ffprobe, which nzbToMedia's check_media uses to validate
# downloaded media (autoProcessMedia.cfg check_media = 1). The linuxserver
# sabnzbd image doesn't bundle it, so install it at container init.
#
# This is a live apk fetch from Alpine's CDN on every start: there is no
# in-fleet apk mirror to point at (only github/apt are Nexus-mirrored), so
# it can't be pinned+mirrored the way nzbToMedia's tarball is. If the
# per-boot fetch ever becomes a problem, the linuxserver-blessed
# replacement is DOCKER_MODS=…/mods:universal-package-install +
# INSTALL_PACKAGES=ffmpeg, which drops this hook entirely.
echo "**** installing ffmpeg ****"
apk add --no-cache ffmpeg

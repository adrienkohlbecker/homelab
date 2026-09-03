#!/usr/bin/env bash
#MISE description="Build the box_deps test fixture from the published box artifact"
#MISE interactive=true
# The seed may warm-reboot on aarch64, which requires the pinned firmware.
#MISE depends=["test:firmware"]
#USAGE flag "--ubuntu... <ubuntu>" help="Ubuntu release codename; repeat to build multiple releases" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\nresolute\n'"
set -euo pipefail

exec uv run python test/build_box_deps.py

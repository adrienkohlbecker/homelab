#!/usr/bin/env bash
set -euo pipefail

# Install the build-only module that clears dracut's power commands immediately
# before ZFSBootMenu installs its pool-exporting implementations.
cp -a \
  /build/dracut-modules/89zbm-power-commands \
  /usr/lib/dracut/modules.d/89zbm-power-commands

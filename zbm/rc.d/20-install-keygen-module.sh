#!/bin/bash
# Build-time hook run by build-init.sh inside the builder container (from
# ${BUILDROOT}/rc.d/*). Installs the 61dropbear-keygen dracut module into the
# container's module search path so dracut picks it up. The module is shipped as
# real files under /build/dracut-modules (kept shellcheck-visible) rather than
# generated inline here.
set -euo pipefail
src=/build/dracut-modules/61dropbear-keygen
dst=/usr/lib/dracut/modules.d/61dropbear-keygen
mkdir -p "$dst"
cp "$src/module-setup.sh" "$src/regen-keys.sh" "$dst/"
chmod +x "$dst/module-setup.sh" "$dst/regen-keys.sh"

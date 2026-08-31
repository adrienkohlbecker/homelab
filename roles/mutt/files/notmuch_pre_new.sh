#!/bin/bash
set -euo pipefail

# An unmounted archive looks empty; scanning it would remove indexed messages.
if ! findmnt --noheadings --mountpoint /mnt/services/mutt/archive --options ro >/dev/null; then
  echo "Refusing to index: /mnt/services/mutt/archive must be mounted read-only" >&2
  exit 1
fi

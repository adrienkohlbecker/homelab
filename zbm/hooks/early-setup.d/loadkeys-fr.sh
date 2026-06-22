#!/bin/bash
set -euo pipefail

# ZBM's run-hooks skips non-executable hooks, so this file must stay +x.
loadkeys -q fr || echo "loadkeys fr failed (keyboard is QWERTY)" >&2

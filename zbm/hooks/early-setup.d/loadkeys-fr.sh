#!/bin/bash
# Executed (not sourced) by ZBM's run-hooks, which skips any hook lacking the
# executable bit — so this file must stay +x or the keymap silently won't load.
# No set -euo pipefail: a keymap failure must not abort boot, and the hook runs
# in its own process so strict mode wouldn't reach ZBM's init anyway; the
# trailing `|| echo` already turns a failure into a warning.
loadkeys -q fr || echo "loadkeys fr failed (keyboard is QWERTY)" >&2

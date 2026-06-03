#!/bin/bash
# Sourced (not executed) by ZBM's run_hookdir — the shebang is inert.
# Do NOT add set -euo pipefail: strict mode would propagate into ZBM's
# init and kill boot on any transient failure.
loadkeys -q fr || echo "loadkeys fr failed (keyboard is QWERTY)" >&2

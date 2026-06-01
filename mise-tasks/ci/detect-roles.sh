#!/usr/bin/env bash
# Thin wrapper: the full pipeline lives in detect.py.
# mise auto-discovers this file as ci:detect-roles; CI calls it unchanged.
exec python3 mise-tasks/ci/detect.py run "$@"

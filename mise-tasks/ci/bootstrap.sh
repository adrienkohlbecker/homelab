#!/usr/bin/env bash
#MISE description="Install the pinned toolchain and sync the Python environment for CI jobs"
set -euo pipefail

if [ -n "${MISE_GITHUB_TOKEN:-}" ]; then
  export GITHUB_TOKEN="$MISE_GITHUB_TOKEN"
fi

mise install
mise exec -- uv sync --locked

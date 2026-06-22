#!/usr/bin/env bash
# Emit NUL-delimited tracked files that still exist in the working tree.
set -euo pipefail

git ls-files -z -- "$@" | while IFS= read -r -d '' path; do
  [[ -e "$path" ]] && printf '%s\0' "$path"
done

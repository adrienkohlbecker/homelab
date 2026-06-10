#!/usr/bin/env bash
# Emit NUL-delimited tracked files matching the given pathspecs, filtered to
# those that still EXIST in the working tree. `git ls-files` lists index
# entries, which include files deleted from the working tree (a pending,
# not-yet-committed deletion); piping those straight into a formatter/linter
# makes it fail with "no such file or directory". Lint/fmt tasks pipe through
# this so they only ever see files that are actually present.
set -euo pipefail

git ls-files -z "$@" | while IFS= read -r -d '' f; do
  if [ -e "$f" ]; then
    printf '%s\0' "$f"
  fi
done
